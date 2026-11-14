// Package tunnel keeps the outbound WebSocket to the server: authentication, buffered
// telemetry with acknowledgements, facts, keepalive and reconnect (proto/agent-protocol.md).
package tunnel

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"sync/atomic"
	"time"

	"github.com/coder/websocket"

	"github.com/lordraw77/janus-ops/agent/internal/buffer"
	"github.com/lordraw77/janus-ops/agent/internal/collect"
	"github.com/lordraw77/janus-ops/agent/internal/config"
	"github.com/lordraw77/janus-ops/agent/internal/identity"
	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

const (
	batchSamples = 60
	window       = 4 // batches in flight before waiting for acks
	factsEvery   = 60 * time.Second
	pingEvery    = 30 * time.Second
	writeTimeout = 15 * time.Second
	stableAfter  = 60 * time.Second // a connection this old resets the backoff
	maxBackoff   = 60 * time.Second
	authBackoff  = 5 * time.Minute
)

type Agent struct {
	Cfg      *config.Config
	ID       *identity.Identity
	Buf      *buffer.Buffer
	Col      *collect.Collector
	Version  string
	Log      *slog.Logger
	interval atomic.Int64 // time.Duration
	fresh    chan struct{}
	// Now is the clock; replaced in tests.
	Now func() time.Time
}

func New(cfg *config.Config, id *identity.Identity, buf *buffer.Buffer, col *collect.Collector, version string, log *slog.Logger) *Agent {
	a := &Agent{Cfg: cfg, ID: id, Buf: buf, Col: col, Version: version, Log: log, fresh: make(chan struct{}, 1), Now: time.Now}
	a.interval.Store(int64(time.Duration(cfg.IntervalS) * time.Second))
	return a
}

// Run collects and ships telemetry until ctx is cancelled.
func (a *Agent) Run(ctx context.Context) error {
	go a.collectLoop(ctx)
	backoff := time.Second
	for ctx.Err() == nil {
		started := time.Now()
		err := a.session(ctx)
		if ctx.Err() != nil {
			return nil
		}
		if time.Since(started) > stableAfter {
			backoff = time.Second
		}
		delay := backoff
		var refused *refusal
		if errors.As(err, &refused) && (refused.close == 4401 || refused.close == 4403) {
			delay = authBackoff
		}
		// full jitter: reconnects of a whole fleet after a server restart spread out
		delay = time.Duration(rand.Int64N(int64(delay)) + int64(time.Second))
		a.Log.Warn("disconnected", "err", err, "retry_in", delay.Round(time.Second), "buffered", a.Buf.Len())
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(delay):
		}
		backoff = min(backoff*2, maxBackoff)
	}
	return nil
}

func (a *Agent) collectLoop(ctx context.Context) {
	a.Col.Sample(a.Now()) // prime the counters: rates need two readings
	for {
		wait := time.Duration(a.interval.Load())
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		sample := a.Col.Sample(a.Now())
		data, err := json.Marshal(sample)
		if err != nil {
			a.Log.Error("encode sample", "err", err)
			continue
		}
		if _, err := a.Buf.Append(sample.TS, data); err != nil {
			a.Log.Error("buffer sample", "err", err)
			continue
		}
		select {
		case a.fresh <- struct{}{}:
		default:
		}
	}
}

type refusal struct {
	close websocket.StatusCode
	code  string
}

func (r *refusal) Error() string { return fmt.Sprintf("refused by server: %s (%d)", r.code, r.close) }

// HTTPClient honours the configured proxy (else HTTPS_PROXY/NO_PROXY) and trusts the system
// CAs plus ca_file.
func HTTPClient(cfg *config.Config) (*http.Client, error) {
	pool, err := x509.SystemCertPool()
	if err != nil || pool == nil {
		pool = x509.NewCertPool()
	}
	if cfg.CAFile != "" {
		pem, err := os.ReadFile(cfg.CAFile)
		if err != nil {
			return nil, err
		}
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("%s: no PEM certificate found", cfg.CAFile)
		}
	}
	proxy := http.ProxyFromEnvironment
	if cfg.Proxy != "" {
		u, err := url.Parse(cfg.Proxy)
		if err != nil {
			return nil, err
		}
		proxy = http.ProxyURL(u)
	}
	tr := &http.Transport{
		Proxy:               proxy,
		TLSClientConfig:     &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12},
		TLSHandshakeTimeout: 15 * time.Second,
	}
	return &http.Client{Transport: tr, Timeout: 0}, nil
}

func (a *Agent) dial(ctx context.Context) (*websocket.Conn, error) {
	client, err := HTTPClient(a.Cfg)
	if err != nil {
		return nil, err
	}
	dctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	conn, _, err := websocket.Dial(dctx, a.Cfg.WebSocketURL(), &websocket.DialOptions{
		HTTPClient: client,
		HTTPHeader: http.Header{"User-Agent": {"janus-agent/" + a.Version}},
	})
	if err != nil {
		return nil, err
	}
	conn.SetReadLimit(proto.MaxMessage)
	return conn, nil
}

func write(ctx context.Context, c *websocket.Conn, v any) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	wctx, cancel := context.WithTimeout(ctx, writeTimeout)
	defer cancel()
	return c.Write(wctx, websocket.MessageText, data)
}

func read(ctx context.Context, c *websocket.Conn) (string, []byte, error) {
	_, data, err := c.Read(ctx)
	if err != nil {
		if status := websocket.CloseStatus(err); status >= 4000 {
			return "", nil, &refusal{close: status, code: closeReason(err)}
		}
		return "", nil, err
	}
	var env proto.Envelope
	if err := json.Unmarshal(data, &env); err != nil {
		return "", nil, fmt.Errorf("bad message from server: %w", err)
	}
	return env.T, data, nil
}

func closeReason(err error) string {
	var ce websocket.CloseError
	if errors.As(err, &ce) {
		return ce.Reason
	}
	return ""
}

// handshake answers the challenge; returns the server's welcome.
func (a *Agent) handshake(ctx context.Context, c *websocket.Conn) (proto.Welcome, error) {
	hctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	var w proto.Welcome
	t, data, err := read(hctx, c)
	if err != nil {
		return w, err
	}
	var ch proto.Challenge
	if t != "challenge" || json.Unmarshal(data, &ch) != nil || ch.Nonce == "" {
		return w, fmt.Errorf("expected a challenge, got %q", t)
	}
	info := a.Col.Info()
	hello := proto.Hello{
		T: "hello", V: proto.Version, NodeID: a.Cfg.NodeID, PublicKey: a.ID.PublicKey(),
		Signature:   a.ID.SignChallenge(ch.Nonce, a.Cfg.NodeID, a.Cfg.Host()),
		EnrollToken: a.Cfg.EnrollToken, Version: a.Version, Arch: runtime.GOARCH,
		OS: info.OS, Kernel: info.Kernel, Hostname: info.Hostname,
		IntervalS: int(time.Duration(a.interval.Load()) / time.Second), Policy: a.Cfg.Policy,
		Features: proto.Features,
	}
	if err := write(hctx, c, hello); err != nil {
		return w, err
	}
	t, data, err = read(hctx, c)
	if err != nil {
		return w, err
	}
	switch t {
	case "welcome":
		if err := json.Unmarshal(data, &w); err != nil {
			return w, err
		}
		return w, nil
	case "error":
		var e proto.Error
		_ = json.Unmarshal(data, &e)
		return w, &refusal{code: e.Code}
	default:
		return w, fmt.Errorf("expected welcome, got %q", t)
	}
}

// session runs one connection until it fails.
func (a *Agent) session(ctx context.Context) error {
	conn, err := a.dial(ctx)
	if err != nil {
		return err
	}
	defer conn.CloseNow()
	welcome, err := a.handshake(ctx, conn)
	if err != nil {
		return err
	}
	if welcome.Enrolled && a.Cfg.EnrollToken != "" {
		if err := a.Cfg.DropEnrollToken(); err != nil {
			a.Log.Warn("could not remove the enrollment token from the configuration", "err", err)
		}
	}
	if welcome.IntervalS >= 5 && welcome.IntervalS <= 15 {
		a.interval.Store(int64(time.Duration(welcome.IntervalS) * time.Second))
	}
	a.Log.Info("connected", "server", a.Cfg.ServerURL, "enrolled", welcome.Enrolled, "buffered", a.Buf.Len())

	sctx, cancel := context.WithCancel(ctx)
	defer cancel()
	acks := make(chan uint64, window*2)
	errs := make(chan error, 3)
	go func() { errs <- a.readLoop(sctx, conn, acks) }()
	go func() { errs <- a.pingLoop(sctx, conn) }()
	go func() { errs <- a.sendLoop(sctx, conn, acks) }()
	err = <-errs
	cancel()
	conn.Close(websocket.StatusNormalClosure, "")
	return err
}

func (a *Agent) readLoop(ctx context.Context, c *websocket.Conn, acks chan<- uint64) error {
	m := newMux(c, a.Cfg.Policy, a.Cfg.DockerSocket, a.Log)
	defer m.closeAll()
	for {
		kind, raw, err := c.Read(ctx)
		if err != nil {
			if status := websocket.CloseStatus(err); status >= 4000 {
				return &refusal{close: status, code: closeReason(err)}
			}
			return err
		}
		if kind == websocket.MessageBinary {
			if err := m.handle(ctx, raw); err != nil {
				return err
			}
			continue
		}
		var env proto.Envelope
		if err := json.Unmarshal(raw, &env); err != nil {
			return fmt.Errorf("bad message from server: %w", err)
		}
		t, data := env.T, raw
		switch t {
		case "ack":
			var ack proto.Ack
			if err := json.Unmarshal(data, &ack); err != nil {
				return err
			}
			select {
			case acks <- ack.Seq:
			case <-ctx.Done():
				return ctx.Err()
			}
		case "error":
			var e proto.Error
			_ = json.Unmarshal(data, &e)
			return &refusal{code: e.Code}
		default:
			// unknown types are ignored: newer servers may send messages this agent predates
		}
	}
}

func (a *Agent) pingLoop(ctx context.Context, c *websocket.Conn) error {
	t := time.NewTicker(pingEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
			pctx, cancel := context.WithTimeout(ctx, writeTimeout)
			err := c.Ping(pctx)
			cancel()
			if err != nil {
				return fmt.Errorf("ping: %w", err)
			}
		}
	}
}

func (a *Agent) sendLoop(ctx context.Context, c *websocket.Conn, acks <-chan uint64) error {
	var sent uint64 // highest sequence sent on this connection
	inflight := 0
	lastFacts := time.Time{}
	factsTick := time.NewTicker(factsEvery)
	defer factsTick.Stop()
	sendFacts := func() error {
		now := a.Now()
		elapsed := time.Duration(0)
		if !lastFacts.IsZero() {
			elapsed = now.Sub(lastFacts)
		}
		lastFacts = now
		return write(ctx, c, proto.Facts{T: "facts", TS: now.UnixMilli(), Data: a.Col.Facts(elapsed)})
	}
	if err := sendFacts(); err != nil {
		return err
	}
	for {
		for inflight < window {
			recs, err := a.Buf.After(sent, batchSamples)
			if err != nil {
				return err
			}
			if len(recs) == 0 {
				break
			}
			msg := proto.Metrics{T: "metrics", Seq: recs[len(recs)-1].Seq}
			for _, r := range recs {
				msg.Samples = append(msg.Samples, r.Data)
			}
			if err := write(ctx, c, msg); err != nil {
				return err
			}
			sent = msg.Seq
			inflight++
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case seq := <-acks:
			if err := a.Buf.Ack(seq); err != nil {
				return err
			}
			inflight = max(0, inflight-1)
		case <-a.fresh:
		case <-factsTick.C:
			if err := sendFacts(); err != nil {
				return err
			}
		}
	}
}
