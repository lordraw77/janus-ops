//go:build !windows

package collect

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// skipDisk filters pseudo devices out of /sys/block.
func skipDisk(name string) bool {
	for _, prefix := range []string{"loop", "ram", "zram", "sr", "fd", "nbd"} {
		if strings.HasPrefix(name, prefix) {
			return true
		}
	}
	return false
}

func (c *Collector) diskMetrics(p *points, elapsed float64, first bool) {
	lines, err := readLines(c.path("diskstats"))
	if err != nil {
		return
	}
	now := map[string]diskCounters{}
	for _, f := range lines {
		if len(f) < 14 {
			continue
		}
		name := f[2]
		if skipDisk(name) {
			continue
		}
		// whole devices only: partitions are not directories of /sys/block
		if _, err := os.Stat(filepath.Join(c.Sys, "block", name)); err != nil {
			continue
		}
		now[name] = diskCounters{
			readSectors:  parseU(f[5]),
			writeSectors: parseU(f[9]),
			ioMs:         parseU(f[12]),
		}
	}
	before := c.disks
	c.disks = now
	if first || elapsed <= 0 {
		return
	}
	for name, n := range now {
		b, ok := before[name]
		if !ok {
			continue
		}
		// diskstats sectors are always 512 bytes, whatever the device's sector size
		p.add("disk.read_bytes_ps", round(delta(n.readSectors, b.readSectors)*512/elapsed), "device", name)
		p.add("disk.write_bytes_ps", round(delta(n.writeSectors, b.writeSectors)*512/elapsed), "device", name)
		util := delta(n.ioMs, b.ioMs) / (elapsed * 1000) * 100
		if util > 100 {
			util = 100
		}
		p.add("disk.util_pct", round(util), "device", name)
	}
}

// Local, block-backed file systems. Network file systems are left out: statfs can hang on
// them, and they are monitored on their server.
var realFS = map[string]bool{
	"ext2": true, "ext3": true, "ext4": true, "xfs": true, "btrfs": true, "zfs": true,
	"vfat": true, "exfat": true, "f2fs": true, "jfs": true, "reiserfs": true, "ntfs": true,
	"ntfs3": true, "bcachefs": true,
}

// unescapeMount decodes the octal escapes of mountinfo (\040 = space).
func unescapeMount(s string) string {
	if !strings.Contains(s, `\`) {
		return s
	}
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' && i+3 < len(s) {
			if v, err := strconv.ParseUint(s[i+1:i+4], 8, 8); err == nil {
				b.WriteByte(byte(v))
				i += 3
				continue
			}
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// Mounts lists real file systems, one per source device (bind mounts collapse to the
// shortest mount point).
func (c *Collector) Mounts() ([]Mount, error) {
	lines, err := readLines(c.path("self", "mountinfo"))
	if err != nil {
		return nil, err
	}
	bySource := map[string]Mount{}
	var order []string
	for _, f := range lines {
		sep := -1
		for i, v := range f {
			if v == "-" {
				sep = i
				break
			}
		}
		if sep < 0 || len(f) < 5 || sep+2 >= len(f) {
			continue
		}
		m := Mount{Point: unescapeMount(f[4]), FSType: f[sep+1], Source: f[sep+2]}
		if !realFS[m.FSType] {
			continue
		}
		key := m.Source + "|" + f[2] // device id: btrfs subvolumes share a source
		if prev, ok := bySource[key]; ok {
			if len(m.Point) < len(prev.Point) {
				bySource[key] = m
			}
			continue
		}
		bySource[key] = m
		order = append(order, key)
	}
	out := make([]Mount, 0, len(order))
	for _, k := range order {
		out = append(out, bySource[k])
	}
	return out, nil
}

func (c *Collector) fsMetrics(p *points) {
	mounts, err := c.Mounts()
	if err != nil {
		return
	}
	for _, m := range mounts {
		st, err := c.FS(m.Point)
		if err != nil || st.Size == 0 {
			continue
		}
		used := float64(st.Size - st.Free)
		// like df: space reserved for root counts as neither used nor available
		p.add("fs.used_pct", pct(used, used+float64(st.Avail)), "mount", m.Point)
		p.add("fs.free_bytes", float64(st.Avail), "mount", m.Point)
		p.add("fs.size_bytes", float64(st.Size), "mount", m.Point)
		if st.Files > 0 {
			p.add("fs.inodes_used_pct", pct(float64(st.Files-st.FilesFree), float64(st.Files)), "mount", m.Point)
		}
	}
}

// Per-container and per-VM plumbing (Docker, Kubernetes CNIs, Proxmox firewall bridges):
// many short-lived interfaces with no meaning of their own.
var virtualIfacePrefixes = []string{"veth", "br-", "cali", "flannel", "cni", "tap", "fwbr", "fwpr", "fwln"}

func skipIface(name string) bool {
	if name == "lo" {
		return true
	}
	for _, p := range virtualIfacePrefixes {
		if strings.HasPrefix(name, p) {
			return true
		}
	}
	return false
}

func (c *Collector) netMetrics(p *points, elapsed float64, first bool) {
	lines, err := readLines(c.path("net", "dev"))
	if err != nil {
		return
	}
	now := map[string]netCounters{}
	for _, f := range lines {
		// "eth0: 123 ..." but also "eth0:123" when the counter is wider than the column
		name, rest, ok := strings.Cut(strings.Join(f, " "), ":")
		if !ok {
			continue // header lines
		}
		v := strings.Fields(rest)
		if len(v) < 9 || skipIface(name) {
			continue
		}
		now[name] = netCounters{rx: parseU(v[0]), tx: parseU(v[8])}
	}
	before := c.nets
	c.nets = now
	if first || elapsed <= 0 {
		return
	}
	for name, n := range now {
		b, ok := before[name]
		if !ok {
			continue
		}
		p.add("net.rx_bytes_ps", round(delta(n.rx, b.rx)/elapsed), "iface", name)
		p.add("net.tx_bytes_ps", round(delta(n.tx, b.tx)/elapsed), "iface", name)
	}
}
