package tunnel

import (
	"bufio"
	"context"
	"encoding/binary"
	"net"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// echoSocket is a fake Docker socket: answers every line with "echo:<line>".
func echoSocket(t *testing.T) string {
	path := filepath.Join(t.TempDir(), "docker.sock")
	ln, err := net.Listen("unix", path)
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
				r := bufio.NewReader(c)
				for {
					line, err := r.ReadString('\n')
					if err != nil {
						return
					}
					if _, err := c.Write([]byte("echo:" + line)); err != nil {
						return
					}
				}
			}()
		}
	}()
	return path
}

type frameMsg struct {
	kind    byte
	id      uint32
	payload string
}

// drive opens stream 7, sends a line and collects the frames the agent sends back.
func drive(t *testing.T, result chan<- []frameMsg) func(ctx context.Context, c *websocket.Conn) {
	return func(ctx context.Context, c *websocket.Conn) {
		_ = c.Write(ctx, websocket.MessageBinary, frame(frameOpen, 7, []byte(`{"kind":"docker"}`)))
		var got []frameMsg
		deadline, cancel := context.WithTimeout(ctx, 5*time.Second)
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
			if f.kind == frameOpenOK {
				_ = c.Write(ctx, websocket.MessageBinary, frame(frameData, 7, []byte("ping\n")))
			}
			if f.kind == frameData && strings.Contains(f.payload, "echo:ping") {
				_ = c.Write(ctx, websocket.MessageBinary, frame(frameClose, 7, nil))
				break
			}
			if f.kind == frameOpenFail {
				break
			}
		}
		result <- got
	}
}

func TestDockerStreamRelaysBytes(t *testing.T) {
	f := newFakeServer(t)
	result := make(chan []frameMsg, 1)
	f.session = drive(t, result)
	a, _ := newAgent(t, f, "policy:\n  allow_docker: true\ndocker_socket: "+echoSocket(t)+"\n")
	_, done := runSession(t, a)
	got := <-result
	if len(got) < 2 || got[0].kind != frameOpenOK || got[0].id != 7 {
		t.Fatalf("frames = %+v", got)
	}
	if last := got[len(got)-1]; last.kind != frameData || last.payload != "echo:ping\n" {
		t.Fatalf("last frame = %+v", last)
	}
	<-done
}

func TestDockerRefusedByLocalPolicy(t *testing.T) {
	f := newFakeServer(t)
	result := make(chan []frameMsg, 1)
	f.session = drive(t, result)
	a, _ := newAgent(t, f, "docker_socket: "+echoSocket(t)+"\n") // allow_docker defaults to false
	_, done := runSession(t, a)
	got := <-result
	if len(got) != 1 || got[0].kind != frameOpenFail || got[0].payload != "docker_not_allowed" {
		t.Fatalf("frames = %+v", got)
	}
	<-done
}

func TestDockerSocketMissing(t *testing.T) {
	f := newFakeServer(t)
	result := make(chan []frameMsg, 1)
	f.session = drive(t, result)
	a, _ := newAgent(t, f, "policy:\n  allow_docker: true\ndocker_socket: /nonexistent/docker.sock\n")
	_, done := runSession(t, a)
	got := <-result
	if len(got) != 1 || got[0].payload != "docker_unavailable" {
		t.Fatalf("frames = %+v", got)
	}
	<-done
}
