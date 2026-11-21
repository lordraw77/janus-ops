//go:build windows

package collect

import (
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

// Windows telemetry: kernel32, iphlpapi and psapi through x/sys/windows, no cgo, no WMI and no
// PowerShell. The metric names match the Linux ones (cpu.usage_pct, mem.used_pct, fs.*, net.*), so
// the dashboards and the checks work unchanged. Not available: load average, iowait and steal
// (they do not exist on Windows), per-disk I/O and systemd units.

var (
	kernel32         = windows.NewLazySystemDLL("kernel32.dll")
	procSystemTimes  = kernel32.NewProc("GetSystemTimes")
	procMemoryStatus = kernel32.NewProc("GlobalMemoryStatusEx")
	procTickCount64  = kernel32.NewProc("GetTickCount64")
	psapi            = windows.NewLazySystemDLL("psapi.dll")
	procProcMemory   = psapi.NewProc("GetProcessMemoryInfo")
)

func newCollector(proc, sys string) *Collector {
	return &Collector{Proc: proc, Sys: sys, FS: statfs, Root: `C:\`}
}

func ft(f windows.Filetime) uint64 { return uint64(f.HighDateTime)<<32 | uint64(f.LowDateTime) }

// readCPU returns the cumulative busy and total time (100 ns units). Kernel time includes idle.
func readCPU() (cpuTimes, bool) {
	var idle, kernel, user windows.Filetime
	r, _, _ := procSystemTimes.Call(
		uintptr(unsafe.Pointer(&idle)), uintptr(unsafe.Pointer(&kernel)), uintptr(unsafe.Pointer(&user)))
	if r == 0 {
		return cpuTimes{}, false
	}
	return cpuTimes{total: ft(kernel) + ft(user), idle: ft(idle), ok: true}, true
}

type memoryStatus struct {
	Length                                             uint32
	Load                                               uint32
	TotalPhys, AvailPhys, TotalPageFile, AvailPageFile uint64
	TotalVirtual, AvailVirtual, AvailExtendedVirtual   uint64
}

func (c *Collector) platformMetrics(p *points, elapsed float64, first bool) {
	if now, ok := readCPU(); ok {
		p.add("cpu.count", float64(runtime.NumCPU()))
		before := c.cpu
		c.cpu = now
		if !first && before.ok {
			if total := delta(now.total, before.total); total > 0 {
				p.add("cpu.usage_pct", pct(total-delta(now.idle, before.idle), total))
			}
		}
	}
	ms := memoryStatus{Length: uint32(unsafe.Sizeof(memoryStatus{}))}
	if r, _, _ := procMemoryStatus.Call(uintptr(unsafe.Pointer(&ms))); r != 0 && ms.TotalPhys > 0 {
		p.add("mem.total_bytes", float64(ms.TotalPhys))
		p.add("mem.available_bytes", float64(ms.AvailPhys))
		p.add("mem.used_pct", pct(float64(ms.TotalPhys-ms.AvailPhys), float64(ms.TotalPhys)))
		// the commit limit is RAM plus the page file: the difference is the page file ("swap")
		if ms.TotalPageFile > ms.TotalPhys {
			total := float64(ms.TotalPageFile - ms.TotalPhys)
			free := float64(ms.AvailPageFile)
			used := total - free
			if used < 0 {
				used = 0
			}
			p.add("swap.used_bytes", used)
			p.add("swap.used_pct", pct(used, total))
		}
	}
	c.fsMetrics(p)
	c.netMetrics(p, elapsed, first)
	if up, err := c.Uptime(); err == nil {
		p.add("system.uptime_s", up)
	}
	if n := processCount(); n > 0 {
		p.add("proc.count", float64(n))
	}
}

// Uptime in seconds since boot.
func (c *Collector) Uptime() (float64, error) {
	r, _, _ := procTickCount64.Call()
	if r == 0 {
		return 0, os.ErrInvalid
	}
	return float64(uint64(r)) / 1000, nil
}

// Mounts lists the fixed drives as C:, D:.
func (c *Collector) Mounts() ([]Mount, error) {
	mask, err := windows.GetLogicalDrives()
	if err != nil {
		return nil, err
	}
	var out []Mount
	for i := 0; i < 26; i++ {
		if mask&(1<<uint(i)) == 0 {
			continue
		}
		letter := string(rune('A'+i)) + ":"
		root, _ := windows.UTF16PtrFromString(letter + `\`)
		if windows.GetDriveType(root) != windows.DRIVE_FIXED {
			continue
		}
		fsType := "ntfs"
		var name [windows.MAX_PATH + 1]uint16
		var typ [windows.MAX_PATH + 1]uint16
		if windows.GetVolumeInformation(root, &name[0], uint32(len(name)), nil, nil, nil, &typ[0], uint32(len(typ))) == nil {
			fsType = strings.ToLower(windows.UTF16ToString(typ[:]))
		}
		out = append(out, Mount{Point: letter, Source: letter, FSType: fsType})
	}
	return out, nil
}

func statfs(path string) (FSStat, error) {
	root, err := windows.UTF16PtrFromString(strings.TrimRight(path, `\`) + `\`)
	if err != nil {
		return FSStat{}, err
	}
	var avail, total, free uint64
	if err := windows.GetDiskFreeSpaceEx(root, &avail, &total, &free); err != nil {
		return FSStat{}, err
	}
	return FSStat{Size: total, Free: free, Avail: avail}, nil
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
		p.add("fs.used_pct", pct(used, used+float64(st.Avail)), "mount", m.Point)
		p.add("fs.free_bytes", float64(st.Avail), "mount", m.Point)
		p.add("fs.size_bytes", float64(st.Size), "mount", m.Point)
	}
}

const (
	ifTypeLoopback = 24
	ifTypeTunnel   = 131
	ifOperUp       = 1
)

func (c *Collector) netMetrics(p *points, elapsed float64, first bool) {
	var table *windows.MibIfTable2
	if err := windows.GetIfTable2Ex(windows.MibIfTableNormal, &table); err != nil || table == nil {
		return
	}
	defer windows.FreeMibTable(unsafe.Pointer(table))
	rows := unsafe.Slice(&table.Table[0], int(table.NumEntries))
	now := map[string]netCounters{}
	for i := range rows {
		r := &rows[i]
		if r.Type == ifTypeLoopback || r.Type == ifTypeTunnel || r.OperStatus != ifOperUp {
			continue
		}
		name := windows.UTF16ToString(r.Alias[:])
		if name == "" {
			continue
		}
		now[name] = netCounters{rx: r.InOctets, tx: r.OutOctets}
	}
	before := c.nets
	c.nets = now
	if first || before == nil || elapsed <= 0 {
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

func processCount() int {
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return 0
	}
	defer windows.CloseHandle(snap)
	e := windows.ProcessEntry32{Size: uint32(unsafe.Sizeof(windows.ProcessEntry32{}))}
	n := 0
	for err := windows.Process32First(snap, &e); err == nil; err = windows.Process32Next(snap, &e) {
		n++
	}
	return n
}

// TopProcesses returns the n busiest processes by CPU (since the previous call) and by memory.
func (c *Collector) TopProcesses(n int, elapsed time.Duration) (byCPU, byMem []proto.Process) {
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil, nil
	}
	defer windows.CloseHandle(snap)
	ticks := map[int]uint64{}
	var all []proto.Process
	e := windows.ProcessEntry32{Size: uint32(unsafe.Sizeof(windows.ProcessEntry32{}))}
	for err := windows.Process32First(snap, &e); err == nil; err = windows.Process32Next(snap, &e) {
		pid := int(e.ProcessID)
		if pid == 0 {
			continue // System Idle Process
		}
		cpuTime, rss, ok := processUsage(e.ProcessID)
		if !ok {
			continue
		}
		ticks[pid] = cpuTime
		cpu := 0.0
		if prev, seen := c.procTicks[pid]; seen && elapsed > 0 {
			// 100 ns units, as a share of one CPU like top(1) does
			cpu = round(delta(cpuTime, prev) / 1e7 / elapsed.Seconds() * 100)
		}
		all = append(all, proto.Process{PID: pid, Name: windows.UTF16ToString(e.ExeFile[:]), CPUPct: cpu, RSSBytes: rss})
	}
	c.procTicks = ticks
	sortProcesses(all, func(a, b proto.Process) bool { return a.CPUPct > b.CPUPct })
	byCPU = describeProcesses(all[:min(n, len(all))])
	sortProcesses(all, func(a, b proto.Process) bool { return a.RSSBytes > b.RSSBytes })
	byMem = describeProcesses(all[:min(n, len(all))])
	return byCPU, byMem
}

type procMemoryCounters struct {
	Cb                         uint32
	PageFaultCount             uint32
	PeakWorkingSetSize         uintptr
	WorkingSetSize             uintptr
	QuotaPeakPagedPoolUsage    uintptr
	QuotaPagedPoolUsage        uintptr
	QuotaPeakNonPagedPoolUsage uintptr
	QuotaNonPagedPoolUsage     uintptr
	PagefileUsage              uintptr
	PeakPagefileUsage          uintptr
}

// processUsage returns the CPU time (kernel+user, 100 ns) and the working set of a process. A
// protected process that cannot be opened is skipped.
func processUsage(pid uint32) (cpu uint64, rss uint64, ok bool) {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil {
		return 0, 0, false
	}
	defer windows.CloseHandle(h)
	var created, exited, kernel, user windows.Filetime
	if windows.GetProcessTimes(h, &created, &exited, &kernel, &user) != nil {
		return 0, 0, false
	}
	pm := procMemoryCounters{Cb: uint32(unsafe.Sizeof(procMemoryCounters{}))}
	if r, _, _ := procProcMemory.Call(uintptr(h), uintptr(unsafe.Pointer(&pm)), uintptr(pm.Cb)); r != 0 {
		rss = uint64(pm.WorkingSetSize)
	}
	return ft(kernel) + ft(user), rss, true
}

// describeProcesses adds the executable path and the owner to the few processes shown.
func describeProcesses(list []proto.Process) []proto.Process {
	out := make([]proto.Process, len(list))
	for i, pr := range list {
		out[i] = pr
		pid := uint32(pr.PID)
		h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
		if err != nil {
			continue
		}
		var buf [windows.MAX_PATH * 2]uint16
		n := uint32(len(buf))
		if windows.QueryFullProcessImageName(h, 0, &buf[0], &n) == nil {
			out[i].Cmd = windows.UTF16ToString(buf[:n])
			if len(out[i].Cmd) > maxCmd {
				out[i].Cmd = out[i].Cmd[:maxCmd]
			}
		}
		var tok windows.Token
		if windows.OpenProcessToken(h, windows.TOKEN_QUERY, &tok) == nil {
			if u, err := tok.GetTokenUser(); err == nil {
				if account, _, _, err := u.User.Sid.LookupAccount(""); err == nil {
					out[i].User = account
				}
			}
			tok.Close()
		}
		windows.CloseHandle(h)
	}
	return out
}

// Listening is not collected on Windows yet.
func (c *Collector) Listening() []proto.Listener { return []proto.Listener{} }

// Info describes the host for the hello message.
func (c *Collector) Info() Info {
	info := Info{OS: "Windows"}
	info.Hostname, _ = os.Hostname()
	v := windows.RtlGetVersion()
	build := strconv.FormatUint(uint64(v.BuildNumber), 10)
	info.Kernel = strconv.FormatUint(uint64(v.MajorVersion), 10) + "." + strconv.FormatUint(uint64(v.MinorVersion), 10) + "." + build
	if k, err := registry.OpenKey(registry.LOCAL_MACHINE, `SOFTWARE\Microsoft\Windows NT\CurrentVersion`, registry.QUERY_VALUE); err == nil {
		defer k.Close()
		if name, _, err := k.GetStringValue("ProductName"); err == nil {
			// the registry still says "Windows 10" on Windows 11 (build 22000 and later)
			if v.BuildNumber >= 22000 {
				name = strings.Replace(name, "Windows 10", "Windows 11", 1)
			}
			info.OS = name
			if dv, _, err := k.GetStringValue("DisplayVersion"); err == nil && dv != "" {
				info.OS += " " + dv
			}
		}
	}
	return info
}

// Services (service discovery by process and configuration files) is Linux-only for now.
func (c *Collector) Services() []proto.Service { return []proto.Service{} }
