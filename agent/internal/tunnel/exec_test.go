package tunnel

import (
	"context"
	"encoding/binary"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// driveExec opens stream 9 with the given request and collects frames until CLOSE or OPEN_FAIL.
func driveExec(req string, result chan<- []frameMsg) func(ctx context.Context, c *websocket.Conn) {
	return func(ctx context.Context, c *websocket.Conn) {
		_ = c.Write(ctx, websocket.MessageBinary, frame(frameOpen, 9, []byte(req)))
		var got []frameMsg
		deadline, cancel := context.WithTimeout(ctx, 15*time.Second)
		defer cancel()
		for {
			kind, data, err := c.Read(deadline)
			if err != nil {
				break
			}
			if kind != websocket.MessageBinary {
				continue // facts, metrics
			}
			f := frameMsg{data[0], binary.BigEndian.Uint32(data[1:5]), string(data[5:])}
			got = append(got, f)
			if f.kind == frameClose || f.kind == frameOpenFail {
				break
			}
		}
		result <- got
	}
}

func runExecCase(t *testing.T, policy, req string) []frameMsg {
	t.Helper()
	f := newFakeServer(t)
	result := make(chan []frameMsg, 1)
	f.session = driveExec(req, result)
	a, _ := newAgent(t, f, policy)
	_, done := runSession(t, a)
	got := <-result
	<-done
	return got
}

const execOn = "policy:\n  allow_exec: true\n"

func TestExecRefusedByLocalPolicy(t *testing.T) {
	got := runExecCase(t, "", `{"kind":"exec","argv":["true"]}`) // allow_exec defaults to false
	if len(got) != 1 || got[0].kind != frameOpenFail || got[0].payload != "exec_not_allowed" {
		t.Fatalf("frames = %+v", got)
	}
}

func TestExecRefusesBadArguments(t *testing.T) {
	for _, req := range []string{
		`{"kind":"exec","argv":[]}`,
		`{"kind":"exec"}`,
		`{"kind":"exec","argv":[""]}`,
	} {
		got := runExecCase(t, execOn, req)
		if len(got) != 1 || got[0].payload != "bad_request" {
			t.Fatalf("%s: frames = %+v", req, got)
		}
	}
}

func TestExecRelaysOutputAndExitCode(t *testing.T) {
	got := runExecCase(t, execOn, `{"kind":"exec","argv":["sh","-c","echo hi; exit 3"]}`)
	if len(got) < 3 || got[0].kind != frameOpenOK {
		t.Fatalf("frames = %+v", got)
	}
	var out strings.Builder
	for _, f := range got[1 : len(got)-1] {
		if f.kind != frameData {
			t.Fatalf("unexpected frame %+v", f)
		}
		out.WriteString(f.payload)
	}
	if out.String() != "hi\n" {
		t.Fatalf("output = %q", out.String())
	}
	if last := got[len(got)-1]; last.kind != frameClose || last.payload != "exit:3" {
		t.Fatalf("close = %+v", last)
	}
}

func TestExecSuccessReportsExitZero(t *testing.T) {
	got := runExecCase(t, execOn, `{"kind":"exec","argv":["true"]}`)
	if last := got[len(got)-1]; last.kind != frameClose || last.payload != "exit:0" {
		t.Fatalf("frames = %+v", got)
	}
}

func TestExecMissingBinaryIsRefused(t *testing.T) {
	got := runExecCase(t, execOn, `{"kind":"exec","argv":["/nonexistent/janus-bin"]}`)
	if len(got) != 1 || got[0].kind != frameOpenFail || got[0].payload != "exec_unavailable" {
		t.Fatalf("frames = %+v", got)
	}
}

func TestExecTimeoutStopsTheCommand(t *testing.T) {
	start := time.Now()
	got := runExecCase(t, execOn, `{"kind":"exec","argv":["sleep","30"],"timeout_s":1}`)
	if elapsed := time.Since(start); elapsed > 10*time.Second {
		t.Fatalf("timeout not enforced: %v", elapsed)
	}
	if last := got[len(got)-1]; last.kind != frameClose || last.payload != "timeout" {
		t.Fatalf("frames = %+v", got)
	}
}
