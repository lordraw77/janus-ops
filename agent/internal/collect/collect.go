// Package collect reads Linux telemetry from procfs and sysfs (FR-22). No cgo, no exec except
// systemctl for failed units.
package collect

import (
	"bufio"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

// clockTicks is USER_HZ: 100 on every Linux amd64/arm64 build (sysconf needs cgo).
const clockTicks = 100

// Collector keeps the previous counters so rates can be computed between cycles.
type Collector struct {
	Proc string // procfs root, normally /proc
	Sys  string // sysfs root, normally /sys
	// FS statfs's a mount point; replaced in tests.
	FS func(path string) (FSStat, error)
	// FailedUnits lists failed systemd units; replaced in tests.
	FailedUnits func() ([]string, error)
	// RunningUnits lists the running systemd services; replaced in tests.
	RunningUnits func() ([]string, error)
	// Root is prepended to the configuration files read for service discovery ("/" normally).
	Root string

	last      time.Time
	cpu       cpuTimes
	disks     map[string]diskCounters
	nets      map[string]netCounters
	procTicks map[int]uint64
}

func New(proc, sys string) *Collector {
	return newCollector(proc, sys)
}

type points struct{ list []proto.Point }

func (p *points) add(name string, v float64, labels ...string) {
	pt := proto.Point{N: name, V: v}
	if len(labels) > 0 {
		pt.L = map[string]string{}
		for i := 0; i+1 < len(labels); i += 2 {
			pt.L[labels[i]] = labels[i+1]
		}
	}
	p.list = append(p.list, pt)
}

// Sample collects one cycle. Rate metrics (CPU, disk, network) appear from the second call.
func (c *Collector) Sample(now time.Time) proto.Sample {
	var p points
	elapsed := now.Sub(c.last).Seconds()
	first := c.last.IsZero()
	c.last = now

	c.platformMetrics(&p, elapsed, first)
	if c.FailedUnits != nil {
		if units, err := c.FailedUnits(); err == nil {
			p.add("systemd.failed_units", float64(len(units)))
		}
	}
	return proto.Sample{TS: now.UnixMilli(), Points: p.list}
}

func (c *Collector) path(parts ...string) string {
	return filepath.Join(append([]string{c.Proc}, parts...)...)
}

// readFields returns the whitespace-separated fields of every line of a file.
func readLines(path string) ([][]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var out [][]string
	s := bufio.NewScanner(f)
	s.Buffer(make([]byte, 64*1024), 1024*1024)
	for s.Scan() {
		out = append(out, strings.Fields(s.Text()))
	}
	return out, s.Err()
}

func parseU(s string) uint64 {
	v, _ := strconv.ParseUint(s, 10, 64)
	return v
}

func parseF(s string) float64 {
	v, _ := strconv.ParseFloat(s, 64)
	return v
}

func pct(part, total float64) float64 {
	if total <= 0 {
		return 0
	}
	return round(part / total * 100)
}

// round keeps two decimals: enough for dashboards, smaller JSON.
func round(v float64) float64 {
	if v < 0 {
		return 0
	}
	return float64(int64(v*100+0.5)) / 100
}

// delta is a counter difference that tolerates resets (reboot, device re-attached).
func delta(now, before uint64) float64 {
	if now < before {
		return 0
	}
	return float64(now - before)
}

// Facts gathers the slower-changing host state sent every minute.
func (c *Collector) Facts(elapsed time.Duration) proto.FactsData {
	var d proto.FactsData
	d.UptimeS, _ = c.Uptime()
	d.FailedUnits = []string{}
	if c.FailedUnits != nil {
		if units, err := c.FailedUnits(); err == nil {
			d.FailedUnits = units
		}
	}
	d.Units = []string{}
	if c.RunningUnits != nil {
		if units, err := c.RunningUnits(); err == nil {
			d.Units = units
		}
	}
	if len(d.Units) > 300 {
		d.Units = d.Units[:300]
	}
	d.Services = c.Services()
	d.Listening = c.Listening()
	d.TopCPU, d.TopMem = c.TopProcesses(10, elapsed)
	return d
}
