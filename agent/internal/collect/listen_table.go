package collect

import (
	"encoding/binary"
	"net"
	"sort"
	"strconv"
	"strings"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

// Parsers of the tables the Windows IP helper returns for the listening sockets
// (GetExtendedTcpTable with TCP_TABLE_OWNER_PID_LISTENER, GetExtendedUdpTable with
// UDP_TABLE_OWNER_PID). They only read bytes, so they build and are tested on every OS; the calls
// that fill the buffers are in listen_windows.go.
//
// A table is a DWORD with the number of rows followed by the rows. In a row the port is a DWORD
// whose low 16 bits hold the port in network byte order, and the owner is a process id.

type listenRow struct {
	proto string
	addr  string
	port  int
	pid   uint32
}

const (
	tcp4RowSize = 24 // state, local addr, local port, remote addr, remote port, pid
	tcp6RowSize = 56 // local addr[16], scope, port, remote addr[16], scope, port, state, pid
	udp4RowSize = 12 // local addr, local port, pid
	udp6RowSize = 28 // local addr[16], scope, port, pid
)

func winPort(v uint32) int {
	p := uint16(v)
	return int(p>>8 | p<<8)
}

// tableRows calls f for each complete row of the table; a short or damaged buffer yields fewer rows.
func tableRows(buf []byte, size int, f func(row []byte)) {
	if len(buf) < 4 {
		return
	}
	n := int(binary.LittleEndian.Uint32(buf))
	for i := 0; i < n && 4+(i+1)*size <= len(buf); i++ {
		f(buf[4+i*size : 4+(i+1)*size])
	}
}

func parseTCP4(buf []byte) []listenRow {
	var out []listenRow
	tableRows(buf, tcp4RowSize, func(r []byte) {
		out = append(out, listenRow{"tcp", net.IP(r[4:8]).String(),
			winPort(binary.LittleEndian.Uint32(r[8:])), binary.LittleEndian.Uint32(r[20:])})
	})
	return out
}

func parseTCP6(buf []byte) []listenRow {
	var out []listenRow
	tableRows(buf, tcp6RowSize, func(r []byte) {
		out = append(out, listenRow{"tcp", net.IP(r[0:16]).String(),
			winPort(binary.LittleEndian.Uint32(r[20:])), binary.LittleEndian.Uint32(r[52:])})
	})
	return out
}

func parseUDP4(buf []byte) []listenRow {
	var out []listenRow
	tableRows(buf, udp4RowSize, func(r []byte) {
		out = append(out, listenRow{"udp", net.IP(r[0:4]).String(),
			winPort(binary.LittleEndian.Uint32(r[4:])), binary.LittleEndian.Uint32(r[8:])})
	})
	return out
}

func parseUDP6(buf []byte) []listenRow {
	var out []listenRow
	tableRows(buf, udp6RowSize, func(r []byte) {
		out = append(out, listenRow{"udp", net.IP(r[0:16]).String(),
			winPort(binary.LittleEndian.Uint32(r[20:])), binary.LittleEndian.Uint32(r[24:])})
	})
	return out
}

// listeners turns table rows into the same list the Linux agent sends: unique, sorted by port,
// the owning process named without its ".exe".
func listeners(rows []listenRow, names map[uint32]string) []proto.Listener {
	seen := map[string]bool{}
	out := []proto.Listener{}
	for _, r := range rows {
		key := r.proto + "|" + r.addr + "|" + strconv.Itoa(r.port)
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, proto.Listener{Proto: r.proto, Addr: r.addr, Port: r.port, Process: names[r.pid]})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Port != out[j].Port {
			return out[i].Port < out[j].Port
		}
		return out[i].Proto+out[i].Addr < out[j].Proto+out[j].Addr
	})
	return out
}

// processName is an executable's file name without ".exe": "nginx.exe" is the process "nginx",
// the name the catalog and the Linux agent use.
func processName(exe string) string {
	if len(exe) > 4 && strings.EqualFold(exe[len(exe)-4:], ".exe") {
		return exe[:len(exe)-4]
	}
	return exe
}
