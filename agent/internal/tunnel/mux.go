package tunnel

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"strconv"
	"sync"
	"time"

	"github.com/coder/websocket"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

// Streams multiplexed over the WebSocket in binary frames (proto/agent-protocol.md):
// [type: 1 byte][stream id: 4 bytes big-endian][payload]. The server opens streams; the agent
// only accepts kinds its local policy allows and never interprets the bytes it relays.
const (
	frameOpen     byte = 1
	frameData     byte = 2
	frameClose    byte = 3
	frameOpenOK   byte = 4
	frameOpenFail byte = 5

	chunkSize     = 32 * 1024
	streamBacklog = 256 // queued writes per stream before it is dropped (protects the tunnel)
	maxStreams    = 64

	// exec streams (F8): a command run without a shell, only when the local policy allows exec
	maxExecs        = 4
	execMaxArgs     = 64
	execMaxArgLen   = 4096
	execMaxOutput   = 256 * 1024
	execDefaultSecs = 60
	execMaxSecs     = 300
	execWaitDelay   = 2 * time.Second
)

type openRequest struct {
	Kind string `json:"kind"`
	// exec only
	Argv     []string `json:"argv,omitempty"`
	TimeoutS int      `json:"timeout_s,omitempty"`
}

type stream struct {
	conn net.Conn
	in   chan []byte
	once sync.Once
}

func (s *stream) close() { s.once.Do(func() { close(s.in); s.conn.Close() }) }

type mux struct {
	ws     *websocket.Conn
	policy proto.Policy
	docker string // Docker Engine API socket
	log    *slog.Logger
	mu     sync.Mutex
	open   map[uint32]*stream
	// dial is replaced in tests
	dial func(ctx context.Context, network, addr string) (net.Conn, error)
	// execSlots bounds the commands running at once
	execSlots chan struct{}
}

func newMux(ws *websocket.Conn, policy proto.Policy, dockerSocket string, log *slog.Logger) *mux {
	var d net.Dialer
	return &mux{
		ws: ws, policy: policy, docker: dockerSocket, log: log, open: map[uint32]*stream{},
		dial: d.DialContext, execSlots: make(chan struct{}, maxExecs),
	}
}

func frame(kind byte, id uint32, payload []byte) []byte {
	b := make([]byte, 5+len(payload))
	b[0] = kind
	binary.BigEndian.PutUint32(b[1:5], id)
	copy(b[5:], payload)
	return b
}

func (m *mux) send(ctx context.Context, kind byte, id uint32, payload []byte) error {
	wctx, cancel := context.WithTimeout(ctx, writeTimeout)
	defer cancel()
	return m.ws.Write(wctx, websocket.MessageBinary, frame(kind, id, payload))
}

// handle processes one binary frame from the server.
func (m *mux) handle(ctx context.Context, data []byte) error {
	if len(data) < 5 {
		return errors.New("short frame")
	}
	kind, id, payload := data[0], binary.BigEndian.Uint32(data[1:5]), data[5:]
	switch kind {
	case frameOpen:
		return m.openStream(ctx, id, payload)
	case frameData:
		// the channel is only closed under m.mu, so the send below cannot hit a closed channel
		full := false
		m.mu.Lock()
		if s := m.open[id]; s != nil {
			select {
			case s.in <- append([]byte(nil), payload...):
			default:
				full = true
			}
		}
		m.mu.Unlock()
		if full {
			m.log.Warn("stream backlog full, closing it", "stream", id)
			m.drop(ctx, id, "backlog")
		}
	case frameClose:
		m.drop(ctx, id, "")
	}
	return nil
}

func (m *mux) refuse(ctx context.Context, id uint32, reason string) error {
	return m.send(ctx, frameOpenFail, id, []byte(reason))
}

func (m *mux) openStream(ctx context.Context, id uint32, payload []byte) error {
	var req openRequest
	if err := json.Unmarshal(payload, &req); err != nil {
		return m.refuse(ctx, id, "bad_request")
	}
	if req.Kind == "exec" {
		return m.startExec(ctx, id, req)
	}
	if req.Kind != "docker" {
		return m.refuse(ctx, id, "unknown_kind")
	}
	if !m.policy.AllowDocker {
		// local policy (C9): only the node's administrator can change it
		return m.refuse(ctx, id, "docker_not_allowed")
	}
	m.mu.Lock()
	full := len(m.open) >= maxStreams
	m.mu.Unlock()
	if full {
		return m.refuse(ctx, id, "too_many_streams")
	}
	dctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	conn, err := m.dial(dctx, "unix", m.docker)
	cancel()
	if err != nil {
		return m.refuse(ctx, id, "docker_unavailable")
	}
	s := &stream{conn: conn, in: make(chan []byte, streamBacklog)}
	m.mu.Lock()
	m.open[id] = s
	m.mu.Unlock()
	if err := m.send(ctx, frameOpenOK, id, nil); err != nil {
		s.close()
		return err
	}
	go m.writer(s)
	go m.reader(ctx, id, s)
	return nil
}

// writer copies server data to the local socket.
func (m *mux) writer(s *stream) {
	for chunk := range s.in {
		if _, err := s.conn.Write(chunk); err != nil {
			s.conn.Close()
			return
		}
	}
}

// reader copies the local socket to the server until either side closes.
func (m *mux) reader(ctx context.Context, id uint32, s *stream) {
	buf := make([]byte, chunkSize)
	for {
		n, err := s.conn.Read(buf)
		if n > 0 {
			if werr := m.send(ctx, frameData, id, buf[:n]); werr != nil {
				break
			}
		}
		if err != nil {
			if !errors.Is(err, io.EOF) && !errors.Is(err, net.ErrClosed) {
				m.log.Debug("stream read", "stream", id, "err", err)
			}
			break
		}
	}
	m.drop(ctx, id, "eof")
}

// drop forgets a stream and tells the server (unless the server closed it).
func (m *mux) drop(ctx context.Context, id uint32, reason string) {
	m.mu.Lock()
	s := m.open[id]
	delete(m.open, id)
	if s != nil {
		s.close()
	}
	m.mu.Unlock()
	if s == nil {
		return
	}
	if reason != "" {
		_ = m.send(ctx, frameClose, id, []byte(reason))
	}
}

func (m *mux) closeAll() {
	m.mu.Lock()
	defer m.mu.Unlock()
	for id, s := range m.open {
		s.close()
		delete(m.open, id)
	}
}

// validArgv: a non-empty command line of bounded size, without NUL bytes.
func validArgv(argv []string) bool {
	if len(argv) == 0 || len(argv) > execMaxArgs {
		return false
	}
	for _, a := range argv {
		if a == "" || len(a) > execMaxArgLen {
			return false
		}
		for i := 0; i < len(a); i++ {
			if a[i] == 0 {
				return false
			}
		}
	}
	return true
}

// startExec runs argv directly (no shell) and relays its output as DATA frames. The stream is
// closed with CLOSE reason "exit:<code>", "timeout" or "output_limit". Nothing is read from the
// server: the stream only carries output back.
func (m *mux) startExec(ctx context.Context, id uint32, req openRequest) error {
	if !m.policy.AllowExec {
		// local policy (C9): only the node's administrator can change it
		return m.refuse(ctx, id, "exec_not_allowed")
	}
	if !validArgv(req.Argv) {
		return m.refuse(ctx, id, "bad_request")
	}
	timeout := time.Duration(req.TimeoutS) * time.Second
	if req.TimeoutS <= 0 {
		timeout = execDefaultSecs * time.Second
	}
	if timeout > execMaxSecs*time.Second {
		timeout = execMaxSecs * time.Second
	}
	select {
	case m.execSlots <- struct{}{}:
	default:
		return m.refuse(ctx, id, "too_many_streams")
	}
	cctx, cancel := context.WithTimeout(ctx, timeout)
	cmd := exec.CommandContext(cctx, req.Argv[0], req.Argv[1:]...)
	cmd.Dir = "/"
	cmd.Env = []string{"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C"}
	cmd.WaitDelay = execWaitDelay
	r, w, err := os.Pipe()
	if err != nil {
		cancel()
		<-m.execSlots
		return m.refuse(ctx, id, "exec_unavailable")
	}
	cmd.Stdout, cmd.Stderr = w, w
	if err := cmd.Start(); err != nil {
		cancel()
		r.Close()
		w.Close()
		<-m.execSlots
		return m.refuse(ctx, id, "exec_unavailable")
	}
	w.Close() // the child holds its own copy: EOF on r comes when the command ends
	if err := m.send(ctx, frameOpenOK, id, nil); err != nil {
		cancel()
		r.Close()
		_ = cmd.Wait()
		<-m.execSlots
		return err
	}
	go m.runExec(ctx, cctx, cancel, id, cmd, r)
	return nil
}

func (m *mux) runExec(ctx, cctx context.Context, cancel context.CancelFunc, id uint32, cmd *exec.Cmd, r *os.File) {
	defer func() { <-m.execSlots }()
	defer cancel()
	buf := make([]byte, chunkSize)
	total, reason := 0, ""
	for {
		n, err := r.Read(buf)
		if n > 0 {
			total += n
			if total > execMaxOutput {
				reason = "output_limit"
				break
			}
			if werr := m.send(ctx, frameData, id, buf[:n]); werr != nil {
				reason = "server_gone"
				break
			}
		}
		if err != nil {
			break
		}
	}
	r.Close()
	if reason != "" {
		cancel() // stop the command: its output is no longer wanted
	}
	waitErr := cmd.Wait()
	switch {
	case reason != "":
	case errors.Is(cctx.Err(), context.DeadlineExceeded):
		reason = "timeout"
	case waitErr == nil:
		reason = "exit:0"
	default:
		var ee *exec.ExitError
		if errors.As(waitErr, &ee) {
			reason = "exit:" + strconv.Itoa(ee.ExitCode())
		} else {
			reason = "exit:-1"
		}
	}
	if ctx.Err() == nil {
		_ = m.send(ctx, frameClose, id, []byte(reason))
	}
}
