package tunnel

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// pendingAgent is an agent whose executable is "new", with "old" kept aside and an update marker.
func pendingAgent(t *testing.T, tries int) (a *Agent, exe string, exited chan int) {
	t.Helper()
	f := newFakeServer(t)
	a, _ = newAgent(t, f, "")
	exe = filepath.Join(t.TempDir(), "janus-agent")
	if err := os.WriteFile(exe, []byte("new"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(exe+".old", []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := writePending(a.Cfg.StateDir, pendingUpdate{Version: "0.3.0", Tries: tries}); err != nil {
		t.Fatal(err)
	}
	exited = make(chan int, 1)
	a.exePath = exe
	a.exit = func(c int) { exited <- c }
	return a, exe, exited
}

func content(t *testing.T, p string) string {
	t.Helper()
	b, _ := os.ReadFile(p)
	return string(b)
}

func TestNoPendingUpdateNoGuard(t *testing.T) {
	f := newFakeServer(t)
	a, _ := newAgent(t, f, "")
	if g := a.guardPendingUpdate(); g != nil {
		t.Fatal("guard without a pending update")
	}
}

func TestConfirmedUpdateCleansUp(t *testing.T) {
	a, exe, exited := pendingAgent(t, 0)
	_ = os.WriteFile(filepath.Join(a.Cfg.StateDir, "update-starts"), []byte("2"), 0o600)
	g := a.guardPendingUpdate()
	if p, _ := readPending(a.Cfg.StateDir); p.Tries != 1 {
		t.Fatalf("tries = %d", p.Tries)
	}
	g.confirm()
	if _, err := os.Stat(pendingPath(a.Cfg.StateDir)); err == nil {
		t.Fatal("marker left")
	}
	if _, err := os.Stat(exe + ".old"); err == nil {
		t.Fatal(".old left")
	}
	if _, err := os.Stat(filepath.Join(a.Cfg.StateDir, "update-starts")); err == nil {
		t.Fatal("start counter left")
	}
	if content(t, exe) != "new" {
		t.Fatal("new binary touched")
	}
	select {
	case <-exited:
		t.Fatal("rolled back although confirmed")
	case <-time.After(300 * time.Millisecond):
	}
}

func TestRollbackAfterRepeatedStartFailures(t *testing.T) {
	a, exe, exited := pendingAgent(t, updateMaxTries) // this start is the fourth
	if g := a.guardPendingUpdate(); g != nil {
		t.Fatal("guard after rollback")
	}
	if c := <-exited; c != 1 {
		t.Fatalf("exit = %d", c)
	}
	if content(t, exe) != "old" {
		t.Fatalf("binary = %q", content(t, exe))
	}
	if _, err := os.Stat(pendingPath(a.Cfg.StateDir)); err == nil {
		t.Fatal("marker left")
	}
}

func TestRollbackWhenTheServerIsNeverReached(t *testing.T) {
	old := updateConfirmWindow
	updateConfirmWindow = 200 * time.Millisecond
	defer func() { updateConfirmWindow = old }()
	a, exe, exited := pendingAgent(t, 0)
	a.guardPendingUpdate()
	select {
	case <-exited:
	case <-time.After(3 * time.Second):
		t.Fatal("no rollback")
	}
	if content(t, exe) != "old" {
		t.Fatalf("binary = %q", content(t, exe))
	}
}

func TestInstallBinaryKeepsTheOldOne(t *testing.T) {
	dir := t.TempDir()
	dst, tmp := filepath.Join(dir, "agent"), filepath.Join(dir, "agent.new")
	_ = os.WriteFile(dst, []byte("old"), 0o755)
	_ = os.WriteFile(tmp, []byte("new"), 0o755)
	if err := installBinary(tmp, dst, dir, "0.3.0"); err != nil {
		t.Fatal(err)
	}
	if content(t, dst) != "new" || content(t, dst+".old") != "old" {
		t.Fatal("binaries not swapped")
	}
	if p, ok := readPending(dir); !ok || p.Version != "0.3.0" {
		t.Fatalf("pending = %+v", p)
	}
}
