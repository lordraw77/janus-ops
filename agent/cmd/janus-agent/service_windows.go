//go:build windows

package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"

	"github.com/lordraw77/janus-ops/agent/internal/config"
)

const (
	serviceName = "JanusAgent"
	installDir  = `C:\Program Files\Janus Agent`
)

func init() {
	extraCommands["install"] = install
	extraCommands["uninstall"] = uninstall
	serviceHook = serviceMain
}

// serviceMain runs the agent as a service when the service control manager started this process.
// It reports false for a console run. It is called from main, not from init: the dispatcher must
// not run while packages are still initialising.
func serviceMain() (int, bool) {
	ok, err := svc.IsWindowsService()
	if err != nil || !ok {
		return 0, false
	}
	path := config.DefaultPath
	for i, a := range os.Args {
		if a == "-config" && i+1 < len(os.Args) {
			path = os.Args[i+1]
		}
	}
	return runService(path), true
}

type handler struct{ path string }

func (h handler) Execute(_ []string, req <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	const accepted = svc.AcceptStop | svc.AcceptShutdown
	status <- svc.Status{State: svc.StartPending}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan int, 1)
	go func() { done <- runContext(ctx, h.path) }()
	status <- svc.Status{State: svc.Running, Accepts: accepted}
	for {
		select {
		case code := <-done: // the agent stopped by itself (configuration error, rollback)
			cancel()
			return false, uint32(code)
		case c := <-req:
			switch c.Cmd {
			case svc.Interrogate:
				status <- c.CurrentStatus
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending}
				cancel()
				select {
				case <-done:
				case <-time.After(20 * time.Second):
				}
				return false, 0
			}
		}
	}
}

// logToFile sends the log to agent.log in the data directory: a service has no console. The file
// restarts empty when it gets over 5 MB, so it cannot fill the disk.
func logToFile(path string) {
	cfg, err := config.Load(path)
	if err != nil {
		return
	}
	file := filepath.Join(cfg.StateDir, "agent.log")
	flags := os.O_WRONLY | os.O_CREATE | os.O_APPEND
	if st, err := os.Stat(file); err == nil && st.Size() > 5<<20 {
		flags = os.O_WRONLY | os.O_CREATE | os.O_TRUNC
	}
	if f, err := os.OpenFile(file, flags, 0o600); err == nil {
		os.Stderr = f
	}
}

func runService(path string) int {
	logToFile(path)
	fmt.Fprintln(os.Stderr, time.Now().Format(time.RFC3339), "service starting, config", path)
	if err := svc.Run(serviceName, handler{path: path}); err != nil {
		fmt.Fprintln(os.Stderr, time.Now().Format(time.RFC3339), "service dispatcher:", err)
		return 1
	}
	return 0
}

// install copies the agent to Program Files, restricts the data directory to SYSTEM and the
// administrators, registers the service (automatic start, restart after a failure) and starts it.
// The configuration file is written by the one-line installer the server generates.
func install(configPath string, _ []string) int {
	cfg, err := config.Load(configPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "configuration:", err)
		return 1
	}
	self, err := os.Executable()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	dst := filepath.Join(installDir, "janus-agent.exe")
	if err := os.MkdirAll(installDir, 0o755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	// the key and the token must not be readable by other local users (ProgramData is open to them)
	for _, dir := range []string{cfg.StateDir, filepath.Dir(configPath)} {
		if out, err := exec.Command("icacls", dir, "/inheritance:r",
			"/grant:r", "*S-1-5-18:(OI)(CI)F", "/grant:r", "*S-1-5-32-544:(OI)(CI)F").CombinedOutput(); err != nil {
			fmt.Fprintf(os.Stderr, "icacls %s: %v\n%s", dir, err, out)
			return 1
		}
	}
	m, err := mgr.Connect()
	if err != nil {
		fmt.Fprintln(os.Stderr, "service manager (run as administrator):", err)
		return 1
	}
	defer m.Disconnect()
	if s, err := m.OpenService(serviceName); err == nil { // reinstall: stop the old one first
		stopService(s)
		s.Close()
	}
	if self != dst {
		if err := copyFile(self, dst); err != nil {
			fmt.Fprintln(os.Stderr, "copy:", err)
			return 1
		}
	}
	s, err := m.OpenService(serviceName)
	if err != nil {
		s, err = m.CreateService(serviceName, dst, mgr.Config{
			DisplayName: "Janus-Ops Agent",
			Description: "Outbound-only telemetry and command agent for Janus-Ops",
			StartType:   mgr.StartAutomatic,
		}, "run", "-config", configPath)
		if err != nil {
			fmt.Fprintln(os.Stderr, "create service:", err)
			return 1
		}
	}
	defer s.Close()
	_ = s.SetRecoveryActions([]mgr.RecoveryAction{
		{Type: mgr.ServiceRestart, Delay: 5 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 15 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 60 * time.Second},
	}, 3600)
	if err := s.Start(); err != nil {
		fmt.Fprintln(os.Stderr, "start:", err)
		return 1
	}
	fmt.Println("Janus agent installed and started:", dst)
	return 0
}

func uninstall(string, []string) int {
	m, err := mgr.Connect()
	if err != nil {
		fmt.Fprintln(os.Stderr, "service manager (run as administrator):", err)
		return 1
	}
	defer m.Disconnect()
	s, err := m.OpenService(serviceName)
	if err != nil {
		fmt.Fprintln(os.Stderr, "the service is not installed")
		return 1
	}
	defer s.Close()
	stopService(s)
	if err := s.Delete(); err != nil {
		fmt.Fprintln(os.Stderr, "delete:", err)
		return 1
	}
	fmt.Println("Janus agent service removed. Files stay in", installDir, "and", config.DefaultPath)
	return 0
}

func stopService(s *mgr.Service) {
	if _, err := s.Control(svc.Stop); err != nil {
		return
	}
	for i := 0; i < 20; i++ {
		if st, err := s.Query(); err != nil || st.State == svc.Stopped {
			return
		}
		time.Sleep(500 * time.Millisecond)
	}
}

func copyFile(from, to string) error {
	in, err := os.Open(from)
	if err != nil {
		return err
	}
	defer in.Close()
	tmp := to + ".new"
	out, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	_ = os.Remove(to + ".old")
	_ = os.Rename(to, to+".old") // a running exe can be renamed, not overwritten
	return os.Rename(tmp, to)
}
