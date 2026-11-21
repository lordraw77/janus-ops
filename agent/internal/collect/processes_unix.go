//go:build !windows

package collect

import (
	"encoding/hex"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

func (c *Collector) pids() ([]int, error) {
	entries, err := os.ReadDir(c.Proc)
	if err != nil {
		return nil, err
	}
	var out []int
	for _, e := range entries {
		if pid, err := strconv.Atoi(e.Name()); err == nil && e.IsDir() {
			out = append(out, pid)
		}
	}
	return out, nil
}

type procStat struct {
	name  string
	ticks uint64
	rss   uint64
}

// readStat parses /proc/<pid>/stat; the command name is in parentheses and may contain spaces.
func (c *Collector) readStat(pid int) (procStat, bool) {
	raw, err := os.ReadFile(c.path(strconv.Itoa(pid), "stat"))
	if err != nil {
		return procStat{}, false
	}
	s := string(raw)
	open, close := strings.IndexByte(s, '('), strings.LastIndexByte(s, ')')
	if open < 0 || close < open {
		return procStat{}, false
	}
	f := strings.Fields(s[close+1:])
	// after ")": state(0) ... utime(11) stime(12) ... rss(21)
	if len(f) < 22 {
		return procStat{}, false
	}
	return procStat{
		name:  s[open+1 : close],
		ticks: parseU(f[11]) + parseU(f[12]),
		rss:   parseU(f[21]) * uint64(os.Getpagesize()),
	}, true
}

// TopProcesses returns the n busiest processes by CPU (since the previous call) and by memory.
func (c *Collector) TopProcesses(n int, elapsed time.Duration) (byCPU, byMem []proto.Process) {
	pids, err := c.pids()
	if err != nil {
		return nil, nil
	}
	ticks := map[int]uint64{}
	var all []proto.Process
	for _, pid := range pids {
		st, ok := c.readStat(pid)
		if !ok {
			continue
		}
		ticks[pid] = st.ticks
		cpu := 0.0
		if prev, ok := c.procTicks[pid]; ok && elapsed > 0 {
			cpu = round(delta(st.ticks, prev) / clockTicks / elapsed.Seconds() * 100)
		}
		all = append(all, proto.Process{PID: pid, Name: st.name, CPUPct: cpu, RSSBytes: st.rss})
	}
	c.procTicks = ticks
	users := c.userNames()
	sort.SliceStable(all, func(i, j int) bool { return all[i].CPUPct > all[j].CPUPct })
	byCPU = c.describe(all[:min(n, len(all))], users)
	sort.SliceStable(all, func(i, j int) bool { return all[i].RSSBytes > all[j].RSSBytes })
	byMem = c.describe(all[:min(n, len(all))], users)
	return byCPU, byMem
}

// describe completes the selected processes with a readable name, command line and user.
// Only the few shown processes are read, not the whole process table.
func (c *Collector) describe(list []proto.Process, users map[string]string) []proto.Process {
	out := make([]proto.Process, 0, len(list))
	for _, p := range list {
		args := c.cmdline(p.PID)
		p.Name = displayName(p.Name, args)
		if len(args) > 0 {
			p.Cmd = strings.Join(args, " ")
			if len(p.Cmd) > maxCmd {
				p.Cmd = p.Cmd[:maxCmd-1] + "…"
			}
		}
		if uid := c.uid(p.PID); uid != "" {
			p.User = users[uid]
			if p.User == "" {
				p.User = uid
			}
		}
		out = append(out, p)
	}
	return out
}

// cmdline returns the arguments of a process; empty for kernel threads.
func (c *Collector) cmdline(pid int) []string {
	raw, err := os.ReadFile(c.path(strconv.Itoa(pid), "cmdline"))
	if err != nil || len(raw) == 0 {
		return nil
	}
	return strings.Split(strings.TrimRight(string(raw), "\x00"), "\x00")
}

// displayName improves the kernel's command name (cut at 15 characters) using the command
// line, and names QEMU/KVM guests by their Proxmox id and name.
func displayName(comm string, args []string) string {
	if len(args) == 0 {
		return comm
	}
	if comm == "kvm" || strings.HasPrefix(comm, "qemu-system") {
		id, name := "", ""
		for i := 0; i+1 < len(args); i++ {
			switch args[i] {
			case "-id":
				id = args[i+1]
			case "-name":
				name, _, _ = strings.Cut(args[i+1], ",")
				name = strings.TrimPrefix(name, "guest=")
			}
		}
		switch {
		case id != "" && name != "":
			return comm + " " + id + " (" + name + ")"
		case name != "":
			return comm + " (" + name + ")"
		case id != "":
			return comm + " " + id
		}
		return comm
	}
	if len(comm) < 15 {
		return comm
	}
	// a title set by the process itself ("pvedaemon worker") or an executable path
	first := args[0]
	if !strings.Contains(first, " ") {
		first = filepath.Base(first)
	}
	if strings.HasPrefix(first, comm) {
		return first
	}
	return comm
}

func (c *Collector) uid(pid int) string {
	lines, err := readLines(c.path(strconv.Itoa(pid), "status"))
	if err != nil {
		return ""
	}
	for _, f := range lines {
		if len(f) >= 2 && f[0] == "Uid:" {
			return f[1] // real uid
		}
	}
	return ""
}

// userNames maps uids to names from the host's /etc/passwd (next to procfs).
func (c *Collector) userNames() map[string]string {
	out := map[string]string{}
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(c.Proc), "etc", "passwd"))
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(raw), "\n") {
		f := strings.Split(line, ":")
		if len(f) >= 3 {
			out[f[2]] = f[0]
		}
	}
	return out
}

// socketOwners maps socket inodes to process names by reading /proc/<pid>/fd.
func (c *Collector) socketOwners() map[string]string {
	owners := map[string]string{}
	pids, err := c.pids()
	if err != nil {
		return owners
	}
	for _, pid := range pids {
		dir := c.path(strconv.Itoa(pid), "fd")
		fds, err := os.ReadDir(dir)
		if err != nil {
			continue
		}
		name := ""
		for _, fd := range fds {
			target, err := os.Readlink(filepath.Join(dir, fd.Name()))
			if err != nil || !strings.HasPrefix(target, "socket:[") {
				continue
			}
			if name == "" {
				st, ok := c.readStat(pid)
				if !ok {
					break
				}
				name = displayName(st.name, c.cmdline(pid))
			}
			owners[strings.TrimSuffix(strings.TrimPrefix(target, "socket:["), "]")] = name
		}
	}
	return owners
}

// decodeAddr turns "0100007F:0016" (little-endian words) into an IP and a port.
func decodeAddr(s string) (string, int, bool) {
	host, port, ok := strings.Cut(s, ":")
	if !ok {
		return "", 0, false
	}
	b, err := hex.DecodeString(host)
	if err != nil || (len(b) != 4 && len(b) != 16) {
		return "", 0, false
	}
	// each 32-bit word is in host (little-endian) order
	for i := 0; i+4 <= len(b); i += 4 {
		b[i], b[i+1], b[i+2], b[i+3] = b[i+3], b[i+2], b[i+1], b[i]
	}
	p, err := strconv.ParseUint(port, 16, 16)
	if err != nil {
		return "", 0, false
	}
	return net.IP(b).String(), int(p), true
}

// Listening lists TCP sockets in LISTEN state and bound UDP sockets.
func (c *Collector) Listening() []proto.Listener {
	owners := c.socketOwners()
	seen := map[string]bool{}
	out := []proto.Listener{}
	for _, src := range []struct{ file, proto, state string }{
		{"tcp", "tcp", "0A"}, {"tcp6", "tcp", "0A"}, {"udp", "udp", "07"}, {"udp6", "udp", "07"},
	} {
		lines, err := readLines(c.path("net", src.file))
		if err != nil {
			continue
		}
		for _, f := range lines {
			if len(f) < 10 || f[3] != src.state || f[0] == "sl" {
				continue
			}
			// UDP state 07 includes connected client sockets: keep only unbound remote ends
			if src.proto == "udp" && !strings.HasPrefix(strings.TrimLeft(f[2], "0"), ":") {
				continue
			}
			addr, port, ok := decodeAddr(f[1])
			if !ok {
				continue
			}
			key := src.proto + addr + strconv.Itoa(port)
			if seen[key] {
				continue
			}
			seen[key] = true
			out = append(out, proto.Listener{Proto: src.proto, Addr: addr, Port: port, Process: owners[f[9]]})
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Port != out[j].Port {
			return out[i].Port < out[j].Port
		}
		return out[i].Proto+out[i].Addr < out[j].Proto+out[j].Addr
	})
	return out
}
