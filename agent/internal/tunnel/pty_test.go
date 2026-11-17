package tunnel

import (
	"context"
	"encoding/binary"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

const ptyOn = "policy:\n  allow_pty: true\n"

func TestPTYRefusedByLocalPolicy(t *testing.T) {
	got := runExecCase(t, "", `{"kind":"pty"}`) // allow_pty defaults to false
	if len(got) != 1 || got[0].kind != frameOpenFail || got[0].payload != "pty_not_allowed" {
		t.Fatalf("frames = %+v", got)
	}
}

func TestPTYRefusesBadSize(t *testing.T) {
	got := runExecCase(t, ptyOn, `{"kind":"pty","cols":3,"rows":3}`)
	if len(got) != 1 || got[0].payload != "bad_request" {
		t.Fatalf("frames = %+v", got)
	}
}

// A shell on a PTY: typed input comes back as output, resize reaches the terminal, and exit
// closes the stream with the exit code.
func TestPTYSessionInputResizeAndExit(t *testing.T) {
	f := newFakeServer(t)
	result := make(chan string, 1)
	f.session = func(ctx context.Context, c *websocket.Conn) {
		_ = c.Write(ctx, websocket.MessageBinary, frame(frameOpen, 5, []byte(`{"kind":"pty","cols":100,"rows":30}`)))
		deadline, cancel := context.WithTimeout(ctx, 20*time.Second)
		defer cancel()
		var out strings.Builder
		sent := 0
		for {
			kind, data, err := c.Read(deadline)
			if err != nil {
				break
			}
			if kind != websocket.MessageBinary {
				continue
			}
			switch data[0] {
			case frameOpenFail:
				result <- "fail:" + string(data[5:])
				return
			case frameOpenOK:
				_ = c.Write(ctx, websocket.MessageBinary, frame(frameData, 5, []byte("stty size; echo DONE$((1+1))\n")))
				rs := make([]byte, 4)
				binary.BigEndian.PutUint16(rs[0:2], 120)
				binary.BigEndian.PutUint16(rs[2:4], 40)
				_ = c.Write(ctx, websocket.MessageBinary, frame(frameResize, 5, rs))
				sent = 1
			case frameData:
				out.Write(data[5:])
				if sent == 1 && strings.Contains(out.String(), "DONE2") {
					sent = 2
					_ = c.Write(ctx, websocket.MessageBinary, frame(frameData, 5, []byte("stty size; exit 7\n")))
				}
			case frameClose:
				result <- out.String() + "|" + string(data[5:])
				return
			}
		}
		result <- out.String() + "|timeout"
	}
	a, _ := newAgent(t, f, ptyOn)
	_, done := runSession(t, a)
	got := <-result
	<-done
	if !strings.Contains(got, "40 120") {
		t.Fatalf("resize not applied: %q", got)
	}
	if !strings.Contains(got, "DONE2") || !strings.HasSuffix(got, "|exit:7") {
		t.Fatalf("output = %q", got)
	}
}
