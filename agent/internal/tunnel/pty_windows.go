//go:build windows

package tunnel

import (
	"context"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// PTY streams on Windows (F8, FR-83): an interactive shell on a pseudo console (ConPTY, Windows 10
// 1809 and later), only when the local policy allows it (allow_pty). The agent runs as the
// SYSTEM account, so the shell does too: the same trust as the root shell on Linux, which is why
// the installer asks for it explicitly. Bytes from the server are the keystrokes; the console
// output (UTF-8 with VT sequences, which xterm.js understands) goes back as DATA frames.
const (
	maxPTYs        = 4
	ptyMaxLifetime = 8 * time.Hour
	ptyDefaultCols = 80
	ptyDefaultRows = 24
	ptyMinSize     = 10
	ptyMaxCols     = 500
	ptyMaxRows     = 200
	// how long to wait for the last output after the shell has exited
	ptyDrainWait = 3 * time.Second
)

func ptySizeOK(cols, rows uint16) bool {
	return cols >= ptyMinSize && cols <= ptyMaxCols && rows >= 5 && rows <= ptyMaxRows
}

// ptyShell is Windows PowerShell, or the command interpreter when it is missing.
func ptyShell() string {
	sys, err := windows.GetSystemDirectory()
	if err != nil {
		return ""
	}
	for _, p := range []string{filepath.Join(sys, "WindowsPowerShell", "v1.0", "powershell.exe"), filepath.Join(sys, "cmd.exe")} {
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p
		}
	}
	return ""
}

// conPTY is a pseudo console with the shell attached to it.
type conPTY struct {
	console windows.Handle
	process windows.Handle
	in      *os.File // keystrokes to the shell
	out     *os.File // what the console prints
	once    sync.Once

	mu   sync.Mutex
	dead bool // the process handle is closed: it must never be used again (a handle value can be reused)
}

func closeHandles(hs ...windows.Handle) {
	for _, h := range hs {
		if h != 0 && h != windows.InvalidHandle {
			_ = windows.CloseHandle(h)
		}
	}
}

// newConPTY starts the shell on a new pseudo console of the given size.
func newConPTY(shell string, cols, rows uint16) (*conPTY, error) {
	var inR, inW, outR, outW windows.Handle
	if err := windows.CreatePipe(&inR, &inW, nil, 0); err != nil {
		return nil, err
	}
	if err := windows.CreatePipe(&outR, &outW, nil, 0); err != nil {
		closeHandles(inR, inW)
		return nil, err
	}
	var console windows.Handle
	size := windows.Coord{X: int16(cols), Y: int16(rows)}
	if err := windows.CreatePseudoConsole(size, inR, outW, 0, &console); err != nil {
		closeHandles(inR, inW, outR, outW)
		return nil, err
	}
	closeHandles(inR, outW) // the console keeps its own copies of these two ends

	attrs, err := windows.NewProcThreadAttributeList(1)
	if err != nil {
		windows.ClosePseudoConsole(console)
		closeHandles(inW, outR)
		return nil, err
	}
	defer attrs.Delete()
	// for this attribute the value is the console handle itself, not a pointer to it
	if err := attrs.Update(windows.PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, *(*unsafe.Pointer)(unsafe.Pointer(&console)), unsafe.Sizeof(console)); err != nil {
		windows.ClosePseudoConsole(console)
		closeHandles(inW, outR)
		return nil, err
	}
	si := windows.StartupInfoEx{
		StartupInfo:             windows.StartupInfo{Cb: uint32(unsafe.Sizeof(windows.StartupInfoEx{}))},
		ProcThreadAttributeList: attrs.List(),
	}
	cmdline, err := windows.UTF16PtrFromString(windows.ComposeCommandLine([]string{shell}))
	if err != nil {
		windows.ClosePseudoConsole(console)
		closeHandles(inW, outR)
		return nil, err
	}
	var dir *uint16
	if drive := os.Getenv("SystemDrive"); drive != "" {
		dir, _ = windows.UTF16PtrFromString(drive + `\`)
	}
	var pi windows.ProcessInformation
	err = windows.CreateProcess(nil, cmdline, nil, nil, false,
		windows.EXTENDED_STARTUPINFO_PRESENT|windows.CREATE_UNICODE_ENVIRONMENT, nil, dir, &si.StartupInfo, &pi)
	if err != nil {
		windows.ClosePseudoConsole(console)
		closeHandles(inW, outR)
		return nil, err
	}
	closeHandles(pi.Thread)
	return &conPTY{
		console: console, process: pi.Process,
		in: os.NewFile(uintptr(inW), "conpty-in"), out: os.NewFile(uintptr(outR), "conpty-out"),
	}, nil
}

func (p *conPTY) resize(cols, rows uint16) {
	_ = windows.ResizePseudoConsole(p.console, windows.Coord{X: int16(cols), Y: int16(rows)})
}

func (p *conPTY) kill() {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.dead {
		_ = windows.TerminateProcess(p.process, 1)
	}
}

// closeConsole closes the pseudo console once; that ends the output pipe. It can block while the
// console flushes, so the caller keeps reading meanwhile.
func (p *conPTY) closeConsole() { p.once.Do(func() { windows.ClosePseudoConsole(p.console) }) }

func (m *mux) startPTY(ctx context.Context, id uint32, req openRequest) error {
	if !m.policy.AllowPTY {
		return m.refuse(ctx, id, "pty_not_allowed") // local policy (C9)
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
	p, err := newConPTY(shell, cols, rows)
	if err != nil {
		<-m.ptySlots
		m.log.Error("conpty failed", "err", err) // Windows older than 10 1809 has no ConPTY
		return m.refuse(ctx, id, "pty_unavailable")
	}
	s := &stream{
		w: p.in, in: make(chan []byte, streamBacklog),
		resize: func(c, r uint16) {
			if ptySizeOK(c, r) {
				p.resize(c, r)
			}
		},
		stop: p.kill,
	}
	m.mu.Lock()
	m.open[id] = s
	m.mu.Unlock()
	if err := m.send(ctx, frameOpenOK, id, nil); err != nil {
		s.close()
		p.kill()
		p.finish()
		<-m.ptySlots
		return err
	}
	go m.writer(s)
	go m.runPTY(ctx, id, p)
	return nil
}

// finish releases everything the shell used.
func (p *conPTY) finish() {
	p.closeConsole()
	_ = p.in.Close()
	_ = p.out.Close()
	p.mu.Lock()
	p.dead = true
	closeHandles(p.process)
	p.mu.Unlock()
}

func (m *mux) runPTY(ctx context.Context, id uint32, p *conPTY) {
	defer func() { <-m.ptySlots }()
	timer := time.AfterFunc(ptyMaxLifetime, p.kill)
	defer timer.Stop()

	gone := make(chan struct{}) // the server cannot be reached any more
	readDone := make(chan struct{})
	go func() {
		defer close(readDone)
		buf := make([]byte, chunkSize)
		for {
			n, err := p.out.Read(buf)
			if n > 0 {
				if werr := m.send(ctx, frameData, id, buf[:n]); werr != nil {
					close(gone)
					p.kill()
					return
				}
			}
			if err != nil {
				return // the console was closed after the shell exited
			}
		}
	}()

	_, _ = windows.WaitForSingleObject(p.process, windows.INFINITE)
	// the console is closed in the background: it waits for its output to be read
	go p.closeConsole()
	select {
	case <-readDone:
	case <-time.After(ptyDrainWait):
	}
	reason := "exit:-1"
	var code uint32
	if windows.GetExitCodeProcess(p.process, &code) == nil {
		reason = "exit:" + strconv.Itoa(int(code))
	}
	select {
	case <-gone:
		reason = "server_gone"
	default:
	}
	p.finish()
	m.drop(ctx, id, reason)
}
