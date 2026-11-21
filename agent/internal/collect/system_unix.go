//go:build !windows

package collect

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

func (c *Collector) readCPU() (cpuTimes, int, error) {
	lines, err := readLines(c.path("stat"))
	if err != nil {
		return cpuTimes{}, 0, err
	}
	var t cpuTimes
	count := 0
	for _, f := range lines {
		if len(f) == 0 {
			continue
		}
		if f[0] == "cpu" && len(f) >= 9 {
			// user nice system idle iowait irq softirq steal (guest is already in user)
			for _, v := range f[1:9] {
				t.total += parseU(v)
			}
			t.idle = parseU(f[4]) + parseU(f[5])
			t.iowait = parseU(f[5])
			t.steal = parseU(f[8])
			t.ok = true
		} else if strings.HasPrefix(f[0], "cpu") {
			count++
		}
	}
	return t, count, nil
}

func (c *Collector) cpuMetrics(p *points, first bool) {
	now, count, err := c.readCPU()
	if err != nil || !now.ok {
		return
	}
	p.add("cpu.count", float64(count))
	before := c.cpu
	c.cpu = now
	if first || !before.ok {
		return
	}
	total := delta(now.total, before.total)
	if total == 0 {
		return
	}
	p.add("cpu.usage_pct", pct(total-delta(now.idle, before.idle), total))
	p.add("cpu.iowait_pct", pct(delta(now.iowait, before.iowait), total))
	p.add("cpu.steal_pct", pct(delta(now.steal, before.steal), total))
}

func (c *Collector) loadMetrics(p *points) {
	lines, err := readLines(c.path("loadavg"))
	if err != nil || len(lines) == 0 || len(lines[0]) < 3 {
		return
	}
	p.add("load.1", parseF(lines[0][0]))
	p.add("load.5", parseF(lines[0][1]))
	p.add("load.15", parseF(lines[0][2]))
}

func (c *Collector) memMetrics(p *points) {
	lines, err := readLines(c.path("meminfo"))
	if err != nil {
		return
	}
	kb := map[string]float64{}
	for _, f := range lines {
		if len(f) >= 2 {
			kb[strings.TrimSuffix(f[0], ":")] = parseF(f[1]) * 1024
		}
	}
	total, avail := kb["MemTotal"], kb["MemAvailable"]
	if total > 0 {
		p.add("mem.total_bytes", total)
		p.add("mem.available_bytes", avail)
		p.add("mem.used_pct", pct(total-avail, total))
	}
	if st := kb["SwapTotal"]; st > 0 {
		used := st - kb["SwapFree"]
		p.add("swap.used_bytes", used)
		p.add("swap.used_pct", pct(used, st))
	}
}

// Uptime in seconds from /proc/uptime.
func (c *Collector) Uptime() (float64, error) {
	lines, err := readLines(c.path("uptime"))
	if err != nil {
		return 0, err
	}
	if len(lines) == 0 || len(lines[0]) == 0 {
		return 0, os.ErrInvalid
	}
	return parseF(lines[0][0]), nil
}

func systemdFailed() ([]string, error) {
	if _, err := os.Stat("/run/systemd/system"); err != nil {
		return nil, err // not booted with systemd
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(
		ctx, "systemctl", "list-units", "--state=failed", "--no-legend", "--plain", "--no-pager",
	).Output()
	if err != nil {
		return nil, err
	}
	return ParseFailedUnits(string(out)), nil
}

func systemdRunning() ([]string, error) {
	if _, err := os.Stat("/run/systemd/system"); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(
		ctx, "systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain", "--no-pager",
	).Output()
	if err != nil {
		return nil, err
	}
	return ParseFailedUnits(string(out)), nil
}

func (c *Collector) Info() Info {
	var info Info
	info.Hostname, _ = os.Hostname()
	if raw, err := os.ReadFile(c.path("sys", "kernel", "osrelease")); err == nil {
		info.Kernel = strings.TrimSpace(string(raw))
	}
	info.OS = osRelease(filepath.Join(filepath.Dir(c.Proc), "etc", "os-release"))
	if info.OS == "" {
		info.OS = osRelease("/etc/os-release")
	}
	return info
}

func osRelease(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(raw), "\n") {
		if v, ok := strings.CutPrefix(line, "PRETTY_NAME="); ok {
			return strings.Trim(v, `"'`)
		}
	}
	return ""
}
