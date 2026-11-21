//go:build !windows

package collect

func newCollector(proc, sys string) *Collector {
	return &Collector{Proc: proc, Sys: sys, FS: statfs, FailedUnits: systemdFailed, RunningUnits: systemdRunning, Root: "/"}
}

// platformMetrics reads procfs and sysfs.
func (c *Collector) platformMetrics(p *points, elapsed float64, first bool) {
	c.cpuMetrics(p, first)
	c.loadMetrics(p)
	c.memMetrics(p)
	c.diskMetrics(p, elapsed, first)
	c.fsMetrics(p)
	c.netMetrics(p, elapsed, first)
	if up, err := c.Uptime(); err == nil {
		p.add("system.uptime_s", up)
	}
	if pids, err := c.pids(); err == nil {
		p.add("proc.count", float64(len(pids)))
	}
}
