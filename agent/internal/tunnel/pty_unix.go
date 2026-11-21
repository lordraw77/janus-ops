//go:build !windows

package tunnel

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"strconv"
	"syscall"
	"time"
)

// PTY streams (F8, FR-83): an interactive shell on a pseudo-terminal, only when the local policy
// allows it (allow_pty). Bytes from the server are the keystrokes; the terminal output goes back
// as DATA frames; RESIZE frames change the window size.
const (
	maxPTYs        = 4
	ptyMaxLifetime = 8 * time.Hour
	ptyDefaultCols = 80
	ptyDefaultRows = 24
	ptyMinSize     = 10
	ptyMaxCols     = 500
	ptyMaxRows     = 200
)

func ptySizeOK(cols, rows uint16) bool {
	return cols >= ptyMinSize && cols <= ptyMaxCols && rows >= 5 && rows <= ptyMaxRows
}

func ptyShell() string {
	for _, p := range []string{"/bin/bash", "/bin/sh"} {
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p
		}
	}
	return ""
}

func (m *mux) startPTY(ctx context.Context, id uint32, req openRequest) error {
	if !m.policy.AllowPTY {
		// local policy (C9): only the node's administrator can change it
		return m.refuse(ctx, id, "pty_not_allowed")
	}
	cols, rows := req.Cols, req.Rows
	if cols == 0 {
		cols = ptyDefaultCols
	}
	if rows == 0 {
		rows = ptyDefaultRows
	}
	if !ptySizeOK(cols, rows) {
		return m.refuse(ctx, id, "bad_request")
	}
	shell := ptyShell()
	if shell == "" {
		return m.refuse(ctx, id, "pty_unavailable")
	}
	select {
	case m.ptySlots <- struct{}{}:
	default:
		return m.refuse(ctx, id, "too_many_streams")
	}
	master, slave, err := openPTY(cols, rows)
	if err != nil {
		<-m.ptySlots
		return m.refuse(ctx, id, "pty_unavailable")
	}
	home := os.Getenv("HOME")
	if home == "" {
		home = "/"
	}
	cmd := exec.Command(shell)
	cmd.Args = []string{"-" + shell[len("/bin/"):]} // a leading dash makes it a login shell
	cmd.Dir = home
	cmd.Env = []string{
		"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		"TERM=xterm-256color", "LANG=C.UTF-8", "HOME=" + home, "SHELL=" + shell,
	}
	cmd.Stdin, cmd.Stdout, cmd.Stderr = slave, slave, slave
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true, Setctty: true, Ctty: 0}
	if err := cmd.Start(); err != nil {
		slave.Close()
		master.Close()
		<-m.ptySlots
		return m.refuse(ctx, id, "pty_unavailable")
	}
	slave.Close() // the shell holds its own copy: reads on the master fail when it exits
	s := &stream{
		w: master, in: make(chan []byte, streamBacklog),
		resize: func(c, r uint16) {
			if ptySizeOK(c, r) {
				_ = setPTYSize(master, c, r)
			}
		},
		stop: func() { _ = cmd.Process.Kill() },
	}
	m.mu.Lock()
	m.open[id] = s
	m.mu.Unlock()
	if err := m.send(ctx, frameOpenOK, id, nil); err != nil {
		s.close()
		master.Close()
		_ = cmd.Wait()
		<-m.ptySlots
		return err
	}
	go m.writer(s)
	go m.runPTY(ctx, id, s, cmd, master)
	return nil
}

func (m *mux) runPTY(ctx context.Context, id uint32, s *stream, cmd *exec.Cmd, master *os.File) {
	defer func() { <-m.ptySlots }()
	timer := time.AfterFunc(ptyMaxLifetime, func() { _ = cmd.Process.Kill() })
	defer timer.Stop()
	buf := make([]byte, chunkSize)
	reason := ""
	for {
		n, err := master.Read(buf)
		if n > 0 {
			if werr := m.send(ctx, frameData, id, buf[:n]); werr != nil {
				reason = "server_gone"
				break
			}
		}
		if err != nil {
			break // EIO when the shell has exited
		}
	}
	if reason != "" {
		_ = cmd.Process.Kill()
	}
	waitErr := cmd.Wait()
	master.Close()
	if reason == "" {
		var ee *exec.ExitError
		switch {
		case waitErr == nil:
			reason = "exit:0"
		case errors.As(waitErr, &ee):
			reason = "exit:" + strconv.Itoa(ee.ExitCode())
		default:
			reason = "exit:-1"
		}
	}
	// the server may have closed the stream already: drop sends CLOSE only if it still exists
	m.drop(ctx, id, reason)
}
