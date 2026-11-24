package tunnel

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Rollback of a self-update (FR-84). Installing an update keeps the previous binary as
// <exe>.old and leaves a marker in the state directory. The new binary removes both once it has
// connected to the server (welcome). If it crashes at start three times, or runs for
// updateConfirmWindow without connecting, the agent puts the old binary back and exits, so that
// systemd restarts the version that worked.
const updateMaxTries = 3

var updateConfirmWindow = 3 * time.Minute // a variable: tests shorten it

type pendingUpdate struct {
	Version string `json:"version"`
	Tries   int    `json:"tries"`
}

func pendingPath(stateDir string) string { return filepath.Join(stateDir, "update-pending.json") }

func writePending(stateDir string, p pendingUpdate) error {
	raw, _ := json.Marshal(p)
	tmp := pendingPath(stateDir) + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, pendingPath(stateDir))
}

func readPending(stateDir string) (pendingUpdate, bool) {
	var p pendingUpdate
	raw, err := os.ReadFile(pendingPath(stateDir))
	if err != nil || json.Unmarshal(raw, &p) != nil {
		return p, false
	}
	return p, true
}

// installBinary swaps the new file in, keeping the old one, and records the pending update.
func installBinary(tmp, dst, stateDir, version string) error {
	old := dst + ".old"
	_ = os.Remove(old)
	_ = os.Remove(dst + ".bad") // left by a rollback on Windows, where the running file cannot be deleted
	if err := os.Rename(dst, old); err != nil {
		return err
	}
	if err := os.Rename(tmp, dst); err != nil {
		_ = os.Rename(old, dst)
		return err
	}
	if err := writePending(stateDir, pendingUpdate{Version: version}); err != nil {
		_ = os.Rename(old, dst) // without the marker the new binary could never be rolled back
		return err
	}
	return nil
}

type updateGuard struct {
	a     *Agent
	mu    sync.Mutex
	timer *time.Timer
}

// guardPendingUpdate runs at start. It returns a guard whose confirm() is called after the first
// successful connection; nil if no update is pending.
func (a *Agent) guardPendingUpdate() *updateGuard {
	p, ok := readPending(a.Cfg.StateDir)
	if !ok {
		return nil
	}
	p.Tries++
	if p.Tries > updateMaxTries {
		a.Log.Error("updated agent keeps failing, rolling back", "version", p.Version)
		a.rollback()
		return nil
	}
	_ = writePending(a.Cfg.StateDir, p)
	g := &updateGuard{a: a}
	g.timer = time.AfterFunc(updateConfirmWindow, func() {
		a.Log.Error("updated agent did not connect in time, rolling back", "version", p.Version)
		a.rollback()
	})
	return g
}

func (g *updateGuard) confirm() {
	if g == nil {
		return
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.timer == nil {
		return
	}
	g.timer.Stop()
	g.timer = nil
	_ = os.Remove(pendingPath(g.a.Cfg.StateDir))
	_ = os.Remove(filepath.Join(g.a.Cfg.StateDir, "update-starts")) // the unit's start counter
	_ = os.Remove(g.a.exePath + ".old")
	g.a.Log.Info("update confirmed", "version", g.a.Version)
}

// rollback restores the previous binary and exits; systemd (or the Windows service recovery) restarts it.
func (a *Agent) rollback() {
	old := a.exePath + ".old"
	if _, err := os.Stat(old); err == nil {
		if err := os.Rename(old, a.exePath); err != nil {
			// Windows will not replace the file of a running program, but it lets it be renamed:
			// move the failing binary aside, then put the old one back
			bad := a.exePath + ".bad"
			_ = os.Remove(bad)
			if os.Rename(a.exePath, bad) == nil {
				if err := os.Rename(old, a.exePath); err != nil {
					_ = os.Rename(bad, a.exePath)
					a.Log.Error("rollback failed", "err", err)
				}
			} else {
				a.Log.Error("rollback failed", "err", err)
			}
		}
	}
	_ = os.Remove(pendingPath(a.Cfg.StateDir))
	a.exit(1)
}
