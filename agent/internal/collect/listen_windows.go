//go:build windows

package collect

import (
	"sort"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

// Listening sockets, running services and service discovery on Windows: iphlpapi, toolhelp and
// the service control manager through x/sys/windows. No cgo, no netstat and no PowerShell.

var (
	iphlpapi     = windows.NewLazySystemDLL("iphlpapi.dll")
	procTCPTable = iphlpapi.NewProc("GetExtendedTcpTable")
	procUDPTable = iphlpapi.NewProc("GetExtendedUdpTable")
)

const (
	afInet                = 2
	afInet6               = 23
	tcpTableOwnerPIDListn = 3 // TCP_TABLE_OWNER_PID_LISTENER
	udpTableOwnerPID      = 1 // UDP_TABLE_OWNER_PID
	errInsufficientBuffer = 122
)

// extendedTable reads one of the IP helper tables, growing the buffer until it fits.
func extendedTable(proc *windows.LazyProc, family, class uintptr) []byte {
	var size uint32
	var buf []byte
	for range 5 {
		var p unsafe.Pointer
		if len(buf) > 0 {
			p = unsafe.Pointer(&buf[0])
		}
		r, _, _ := proc.Call(uintptr(p), uintptr(unsafe.Pointer(&size)), 1, family, class, 0)
		switch r {
		case 0:
			return buf[:size]
		case errInsufficientBuffer:
			buf = make([]byte, size+4096) // the table can grow between the two calls
		default:
			return nil
		}
	}
	return nil
}

// processNames maps every process id to its executable name ("nginx.exe").
func processNames() map[uint32]string {
	out := map[uint32]string{}
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return out
	}
	defer windows.CloseHandle(snap)
	e := windows.ProcessEntry32{Size: uint32(unsafe.Sizeof(windows.ProcessEntry32{}))}
	for err := windows.Process32First(snap, &e); err == nil; err = windows.Process32Next(snap, &e) {
		out[e.ProcessID] = windows.UTF16ToString(e.ExeFile[:])
	}
	return out
}

// Listening returns the TCP and UDP sockets in the listening state, with the owning process.
func (c *Collector) Listening() []proto.Listener {
	var rows []listenRow
	rows = append(rows, parseTCP4(extendedTable(procTCPTable, afInet, tcpTableOwnerPIDListn))...)
	rows = append(rows, parseTCP6(extendedTable(procTCPTable, afInet6, tcpTableOwnerPIDListn))...)
	rows = append(rows, parseUDP4(extendedTable(procUDPTable, afInet, udpTableOwnerPID))...)
	rows = append(rows, parseUDP6(extendedTable(procUDPTable, afInet6, udpTableOwnerPID))...)
	names := map[uint32]string{}
	for pid, exe := range processNames() {
		names[pid] = processName(exe)
	}
	return listeners(rows, names)
}

// imagePath is the full path of a process's executable, or "" when it cannot be opened.
func imagePath(pid uint32) string {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil {
		return ""
	}
	defer windows.CloseHandle(h)
	var buf [windows.MAX_PATH * 2]uint16
	n := uint32(len(buf))
	if windows.QueryFullProcessImageName(h, 0, &buf[0], &n) != nil {
		return ""
	}
	return windows.UTF16ToString(buf[:n])
}

// Services returns the catalog services whose process is running.
func (c *Collector) Services() []proto.Service {
	return windowsServices(processNames(), c.Listening(), imagePath)
}

const (
	scManagerEnumerate = 0x0004 // SC_MANAGER_ENUMERATE_SERVICE
	scEnumProcessInfo  = 0      // SC_ENUM_PROCESS_INFO
	serviceWin32       = 0x30   // SERVICE_WIN32
	serviceActive      = 0x1    // SERVICE_ACTIVE
	errMoreData        = 234
	maxUnits           = 300
)

// runningServices lists the Windows services that are running (the counterpart of the running
// systemd units): the service names, such as "MySQL80" or "postgresql-x64-16".
func runningServices() ([]string, error) {
	scm, err := windows.OpenSCManager(nil, nil, scManagerEnumerate)
	if err != nil {
		return nil, err
	}
	defer windows.CloseServiceHandle(scm)
	var buf []byte
	var need, count, resume uint32
	for range 5 {
		var p *byte
		if len(buf) > 0 {
			p = &buf[0]
		}
		err = windows.EnumServicesStatusEx(scm, scEnumProcessInfo, serviceWin32, serviceActive,
			p, uint32(len(buf)), &need, &count, &resume, nil)
		if err == nil {
			break
		}
		if errno, ok := err.(windows.Errno); !ok || errno != errMoreData || need == 0 {
			return nil, err
		}
		buf = make([]byte, need+1024)
		resume = 0
	}
	if err != nil || count == 0 || len(buf) == 0 {
		return []string{}, err
	}
	entries := unsafe.Slice((*windows.ENUM_SERVICE_STATUS_PROCESS)(unsafe.Pointer(&buf[0])), count)
	names := make([]string, 0, count)
	for _, e := range entries {
		if e.ServiceName != nil {
			names = append(names, windows.UTF16PtrToString(e.ServiceName))
		}
	}
	sort.Strings(names)
	if len(names) > maxUnits {
		names = names[:maxUnits]
	}
	return names, nil
}
