package collect

import (
	"encoding/binary"
	"testing"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

func table(size int, rows ...[]byte) []byte {
	b := make([]byte, 4)
	binary.LittleEndian.PutUint32(b, uint32(len(rows)))
	for _, r := range rows {
		row := make([]byte, size)
		copy(row, r)
		b = append(b, row...)
	}
	return b
}

// netPort is a port as the IP helper stores it: network byte order in the low 16 bits.
func netPort(p uint16) []byte {
	return []byte{byte(p >> 8), byte(p), 0, 0}
}

func TestWinPort(t *testing.T) {
	for _, p := range []int{22, 80, 443, 5432, 49152, 65535} {
		v := binary.LittleEndian.Uint32(netPort(uint16(p)))
		if got := winPort(v); got != p {
			t.Errorf("winPort for %d = %d", p, got)
		}
	}
}

func TestParseTCP4(t *testing.T) {
	row := make([]byte, tcp4RowSize)
	copy(row[4:], []byte{0, 0, 0, 0}) // 0.0.0.0
	copy(row[8:], netPort(5432))
	binary.LittleEndian.PutUint32(row[20:], 4242)
	rows := parseTCP4(table(tcp4RowSize, row))
	if len(rows) != 1 || rows[0] != (listenRow{"tcp", "0.0.0.0", 5432, 4242}) {
		t.Fatalf("%+v", rows)
	}
}

func TestParseTCP6(t *testing.T) {
	row := make([]byte, tcp6RowSize)
	row[15] = 1 // ::1
	copy(row[20:], netPort(8080))
	binary.LittleEndian.PutUint32(row[52:], 7)
	rows := parseTCP6(table(tcp6RowSize, row))
	if len(rows) != 1 || rows[0] != (listenRow{"tcp", "::1", 8080, 7}) {
		t.Fatalf("%+v", rows)
	}
}

func TestParseUDP(t *testing.T) {
	r4 := make([]byte, udp4RowSize)
	copy(r4[0:], []byte{127, 0, 0, 1})
	copy(r4[4:], netPort(53))
	binary.LittleEndian.PutUint32(r4[8:], 9)
	r6 := make([]byte, udp6RowSize)
	copy(r6[20:], netPort(5353))
	binary.LittleEndian.PutUint32(r6[24:], 10)
	a, b := parseUDP4(table(udp4RowSize, r4)), parseUDP6(table(udp6RowSize, r6))
	if len(a) != 1 || a[0] != (listenRow{"udp", "127.0.0.1", 53, 9}) {
		t.Fatalf("%+v", a)
	}
	if len(b) != 1 || b[0] != (listenRow{"udp", "::", 5353, 10}) {
		t.Fatalf("%+v", b)
	}
}

func TestDamagedTablesGiveFewerRows(t *testing.T) {
	full := table(tcp4RowSize, make([]byte, tcp4RowSize), make([]byte, tcp4RowSize))
	if n := len(parseTCP4(full[:4+tcp4RowSize+5])); n != 1 { // the second row is cut
		t.Fatalf("%d rows", n)
	}
	huge := make([]byte, 8)
	binary.LittleEndian.PutUint32(huge, 1<<30) // claims more rows than the buffer holds
	if n := len(parseTCP4(huge)); n != 0 {
		t.Fatalf("%d rows", n)
	}
	if parseTCP4(nil) != nil || parseUDP6([]byte{1}) != nil {
		t.Fatal("a buffer that is too short must give nothing")
	}
}

func TestListenersAreUniqueSortedAndNamed(t *testing.T) {
	rows := []listenRow{
		{"tcp", "0.0.0.0", 5432, 1}, {"tcp", "::", 80, 2}, {"tcp", "0.0.0.0", 5432, 1}, {"udp", "0.0.0.0", 80, 3},
	}
	got := listeners(rows, map[uint32]string{1: "postgres", 2: "System"})
	want := []proto.Listener{
		{Proto: "tcp", Addr: "::", Port: 80, Process: "System"},
		{Proto: "udp", Addr: "0.0.0.0", Port: 80},
		{Proto: "tcp", Addr: "0.0.0.0", Port: 5432, Process: "postgres"},
	}
	if len(got) != len(want) {
		t.Fatalf("%+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("row %d: %+v, want %+v", i, got[i], want[i])
		}
	}
}

func TestProcessName(t *testing.T) {
	for in, want := range map[string]string{"nginx.exe": "nginx", "Redis-Server.EXE": "Redis-Server", "svchost": "svchost", ".exe": ".exe"} {
		if got := processName(in); got != want {
			t.Errorf("processName(%q) = %q", in, got)
		}
	}
}

func TestWindowsServicesByProcessName(t *testing.T) {
	procs := map[uint32]string{
		10: "System", 200: "postgres.exe", 100: "postgres.exe", 300: "Redis-Server.exe", 400: "svchost.exe", 500: "nginx.exe",
	}
	listening := []proto.Listener{
		{Proto: "tcp", Port: 6379, Process: "Redis-Server"}, {Proto: "tcp", Port: 5433, Process: "postgres"},
		{Proto: "tcp", Port: 5432, Process: "postgres"}, {Proto: "udp", Port: 53, Process: "postgres"},
	}
	path := func(pid uint32) string {
		if pid == 100 {
			return `C:\Program Files\PostgreSQL\16\bin\postgres.exe`
		}
		return ""
	}
	got := windowsServices(procs, listening, path)
	if len(got) != 3 || got[0].Name != "nginx" || got[1].Name != "postgresql" || got[2].Name != "redis" {
		t.Fatalf("%+v", got)
	}
	pg := got[1]
	if pg.Process != "postgres" || pg.Config["port"] != "5432" || pg.Cmd != `C:\Program Files\PostgreSQL\16\bin\postgres.exe` {
		t.Fatalf("postgres: %+v", pg) // the lowest pid is the main process, the lowest tcp port is the port
	}
	if got[0].Config["port"] != "" || got[2].Config["port"] != "6379" {
		t.Fatalf("ports: %+v", got)
	}
}
