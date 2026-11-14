package tunnel

import (
	"bufio"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/lordraw77/janus-ops/agent/internal/buffer"
	"github.com/lordraw77/janus-ops/agent/internal/collect"
	"github.com/lordraw77/janus-ops/agent/internal/config"
	"github.com/lordraw77/janus-ops/agent/internal/identity"
	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

const nodeID = "5f0c7a52-3a43-4d43-8bd5-1b2f8f4b7b1e"

// fakeServer speaks the server side of the protocol.
type fakeServer struct {
	t        *testing.T
	srv      *httptest.Server
	token    string
	key      atomic.Value // ed25519.PublicKey once enrolled
	ackAfter int          // drop the connection without acking the first N batches
	refuse   websocket.StatusCode

	mu      sync.Mutex
	samples []int64 // timestamps received, in order
	hellos  []proto.Hello
	facts   int
	batches int
}

func newFakeServer(t *testing.T) *fakeServer {
	f := &fakeServer{t: t, token: "tok-123"}
	f.srv = httptest.NewTLSServer(http.HandlerFunc(f.handle))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeServer) handle(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/api/v1/agent/ws" || !strings.HasPrefix(r.UserAgent(), "janus-agent/") {
		http.NotFound(w, r)
		return
	}
	c, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	defer c.CloseNow()
	ctx := r.Context()
	send := func(v any) { data, _ := json.Marshal(v); _ = c.Write(ctx, websocket.MessageText, data) }
	nonce := base64.StdEncoding.EncodeToString([]byte("0123456789abcdef0123456789abcdef"))
	send(proto.Challenge{T: "challenge", V: 1, Nonce: nonce})
	_, data, err := c.Read(ctx)
	if err != nil {
		return
	}
	var h proto.Hello
	if err := json.Unmarshal(data, &h); err != nil {
		return
	}
	f.mu.Lock()
	f.hellos = append(f.hellos, h)
	f.mu.Unlock()
	if f.refuse != 0 {
		send(proto.Error{T: "error", Code: "unauthorized"})
		c.Close(f.refuse, "unauthorized")
		return
	}
	pub, _ := base64.StdEncoding.DecodeString(h.PublicKey)
	sig, _ := base64.StdEncoding.DecodeString(h.Signature)
	if !ed25519.Verify(pub, identity.ChallengeMessage(nonce, h.NodeID, r.Host), sig) {
		c.Close(4401, "unauthorized")
		return
	}
	enrolled := false
	if known, ok := f.key.Load().(ed25519.PublicKey); !ok || !known.Equal(ed25519.PublicKey(pub)) {
		if h.EnrollToken != f.token {
			c.Close(4401, "unauthorized")
			return
		}
		f.key.Store(ed25519.PublicKey(pub))
		enrolled = true
	}
	send(proto.Welcome{T: "welcome", V: 1, IntervalS: 10, Enrolled: enrolled})
	for {
		_, data, err := c.Read(ctx)
		if err != nil {
			return
		}
		var env proto.Envelope
		_ = json.Unmarshal(data, &env)
		switch env.T {
		case "facts":
			f.mu.Lock()
			f.facts++
			f.mu.Unlock()
		case "metrics":
			var m struct {
				Seq     uint64         `json:"seq"`
				Samples []proto.Sample `json:"samples"`
			}
			if err := json.Unmarshal(data, &m); err != nil {
				f.t.Errorf("bad metrics: %v", err)
				return
			}
			f.mu.Lock()
			f.batches++
			drop := f.batches <= f.ackAfter
			for _, s := range m.Samples {
				f.samples = append(f.samples, s.TS)
			}
			f.mu.Unlock()
			if drop {
				return // connection lost before the ack
			}
			send(proto.Ack{T: "ack", Seq: m.Seq})
		}
	}
}

func (f *fakeServer) received() []int64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]int64{}, f.samples...)
}

// caFile writes the test server's certificate as the agent's ca_file.
func (f *fakeServer) caFile(dir string) string {
	path := filepath.Join(dir, "ca.pem")
	cert := f.srv.Certificate()
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw}), 0o600); err != nil {
		f.t.Fatal(err)
	}
	return path
}

func newAgent(t *testing.T, f *fakeServer, extra string) (*Agent, string) {
	t.Helper()
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.yaml")
	cfg := "server_url: " + f.srv.URL + "\nnode_id: " + nodeID + "\nenroll_token: " + f.token +
		"\nca_file: " + f.caFile(dir) + "\nstate_dir: " + dir + "\n" + extra
	if err := os.WriteFile(cfgPath, []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	c, err := config.Load(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	id, err := identity.LoadOrCreate(dir)
	if err != nil {
		t.Fatal(err)
	}
	buf, err := buffer.Open(filepath.Join(dir, "buffer.db"), time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { buf.Close() })
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	return New(c, id, buf, collect.New("/proc", "/sys"), "test", log), cfgPath
}

func fill(t *testing.T, a *Agent, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		data, _ := json.Marshal(proto.Sample{TS: int64(1000 + i), Points: []proto.Point{{N: "cpu.usage_pct", V: 1}}})
		if _, err := a.Buf.Append(int64(1000+i), data); err != nil {
			t.Fatal(err)
		}
	}
}

func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func runSession(t *testing.T, a *Agent) (context.CancelFunc, <-chan error) {
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- a.session(ctx) }()
	t.Cleanup(cancel)
	return cancel, done
}

func TestEnrollShipAndAck(t *testing.T) {
	f := newFakeServer(t)
	a, cfgPath := newAgent(t, f, "")
	fill(t, a, 130) // three batches: 60 + 60 + 10
	cancel, done := runSession(t, a)
	eventually(t, "all samples acknowledged", func() bool { return a.Buf.Len() == 0 })
	cancel()
	<-done

	if got := f.received(); len(got) != 130 || got[0] != 1000 || got[129] != 1129 {
		t.Fatalf("server got %d samples", len(got))
	}
	h := f.hellos[0]
	if h.EnrollToken != "tok-123" || h.NodeID != nodeID || h.Version != "test" || h.Arch == "" {
		t.Fatalf("hello = %+v", h)
	}
	if f.facts == 0 {
		t.Fatal("facts are sent on connect")
	}
	// the one-time token is removed once the server confirmed the enrollment
	raw, _ := os.ReadFile(cfgPath)
	if strings.Contains(string(raw), "tok-123") {
		t.Fatalf("token still in config:\n%s", raw)
	}

	// next connection authenticates with the key alone
	fill(t, a, 1)
	cancel, done = runSession(t, a)
	eventually(t, "second session", func() bool { return a.Buf.Len() == 0 })
	cancel()
	<-done
	if f.hellos[1].EnrollToken != "" {
		t.Fatal("token sent again after enrollment")
	}
}

func TestUnackedSamplesAreResentAfterReconnect(t *testing.T) {
	f := newFakeServer(t)
	f.ackAfter = 1 // first batch arrives but is never acknowledged
	a, _ := newAgent(t, f, "")
	fill(t, a, 5)
	_, done := runSession(t, a)
	if err := <-done; err == nil {
		t.Fatal("session must fail when the server drops it")
	}
	if a.Buf.Len() != 5 {
		t.Fatalf("unacked samples must stay buffered, len=%d", a.Buf.Len())
	}
	cancel, done := runSession(t, a)
	eventually(t, "resend", func() bool { return a.Buf.Len() == 0 })
	cancel()
	<-done
	got := f.received()
	if len(got) != 10 || got[5] != 1000 {
		t.Fatalf("expected the 5 samples twice, got %v", got)
	}
}

func TestRefusalIsReported(t *testing.T) {
	f := newFakeServer(t)
	f.refuse = 4403
	a, _ := newAgent(t, f, "")
	_, done := runSession(t, a)
	err := <-done
	r, ok := err.(*refusal)
	if !ok || r.code != "unauthorized" {
		t.Fatalf("err = %#v", err)
	}
}

func TestUntrustedServerCertificateFails(t *testing.T) {
	f := newFakeServer(t)
	a, _ := newAgent(t, f, "")
	a.Cfg.CAFile = ""
	_, done := runSession(t, a)
	if err := <-done; err == nil || !strings.Contains(err.Error(), "certificate") {
		t.Fatalf("err = %v", err)
	}
	if len(f.hellos) != 0 {
		t.Fatal("nothing may be sent to an unverified server")
	}
}

// connectProxy is a minimal HTTP CONNECT proxy that counts tunnels.
func connectProxy(t *testing.T) (string, *atomic.Int32) {
	var tunnels atomic.Int32
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer c.Close()
				req, err := http.ReadRequest(bufio.NewReader(c))
				if err != nil || req.Method != http.MethodConnect {
					return
				}
				up, err := net.Dial("tcp", req.Host)
				if err != nil {
					return
				}
				defer up.Close()
				tunnels.Add(1)
				_, _ = io.WriteString(c, "HTTP/1.1 200 Connection established\r\n\r\n")
				go func() { _, _ = io.Copy(up, c) }()
				_, _ = io.Copy(c, up)
			}()
		}
	}()
	return "http://" + ln.Addr().String(), &tunnels
}

func TestThroughHTTPProxy(t *testing.T) {
	f := newFakeServer(t)
	proxyURL, tunnels := connectProxy(t)
	a, _ := newAgent(t, f, "proxy: "+proxyURL+"\n")
	fill(t, a, 3)
	cancel, done := runSession(t, a)
	eventually(t, "samples via proxy", func() bool { return a.Buf.Len() == 0 })
	cancel()
	<-done
	if tunnels.Load() != 1 {
		t.Fatalf("CONNECT tunnels = %d", tunnels.Load())
	}
}
