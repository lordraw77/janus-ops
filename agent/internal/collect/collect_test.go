//go:build !windows

package collect

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

var sprintf = fmt.Sprintf

// fakeHost writes a minimal procfs/sysfs tree; files can be replaced between samples.
type fakeHost struct {
	t         *testing.T
	proc, sys string
}

func newFakeHost(t *testing.T) *fakeHost {
	root := t.TempDir()
	h := &fakeHost{t: t, proc: filepath.Join(root, "proc"), sys: filepath.Join(root, "sys")}
	h.write("sys/block/sda/size", "1")
	h.write("sys/block/loop0/size", "1")
	h.write("proc/loadavg", "0.50 0.25 0.10 1/200 1234\n")
	h.write("proc/uptime", "3600.50 7000.00\n")
	h.write("proc/meminfo", "MemTotal:       1000 kB\nMemFree:         100 kB\nMemAvailable:    250 kB\nSwapTotal:       400 kB\nSwapFree:        300 kB\n")
	h.write("proc/self/mountinfo", ""+
		"22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"+
		"23 22 8:1 /srv /mnt/bind rw,relatime shared:1 - ext4 /dev/sda1 rw\n"+
		"24 22 0:5 / /proc rw - proc proc rw\n"+
		"25 22 8:2 / /mnt/my\\040disk rw - xfs /dev/sda2 rw\n"+
		"26 22 0:40 / /mnt/nfs rw - nfs4 srv:/x rw\n")
	h.write("proc/1/stat", "1 (systemd) S 0 1 1 0 -1 4194560 100 0 0 0 50 50 0 0 20 0 1 0 10 100000 25 18446744073709551615\n")
	h.write("proc/42/stat", "42 (my (odd) cmd) R 1 42 42 0 -1 4194560 100 0 0 0 10 0 0 0 20 0 1 0 10 100000 1000 18446744073709551615\n")
	h.cycle(0)
	return h
}

func (h *fakeHost) write(rel, content string) {
	path := filepath.Join(filepath.Dir(h.proc), rel)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		h.t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		h.t.Fatal(err)
	}
}

// cycle writes counters that grow by a fixed amount per step.
func (h *fakeHost) cycle(step uint64) {
	n := func(base, per uint64) uint64 { return base + per*step }
	h.write("proc/stat", sprintf("cpu  %d 0 %d %d %d 0 0 %d 0 0\ncpu0 1 0 1 1 0 0 0 0 0 0\ncpu1 1 0 1 1 0 0 0 0 0 0\nintr 1\n",
		n(100, 30), n(100, 10), n(1000, 50), n(10, 5), n(0, 5)))
	h.write("proc/diskstats", sprintf(""+
		"   8       0 sda %d 0 %d 0 %d 0 %d 0 0 %d 0\n"+
		"   8       1 sda1 1 0 1 0 1 0 1 0 0 1 0\n"+
		"   7       0 loop0 1 0 1 0 1 0 1 0 0 1 0\n",
		n(0, 1), n(0, 2000), n(0, 1), n(0, 4000), n(0, 5000)))
	h.write("proc/net/dev", sprintf(""+
		"Inter-|   Receive                                                |  Transmit\n"+
		" face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"+
		"    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n"+
		"  eth0:%d 1 0 0 0 0 0 0 %d 1 0 0 0 0 0 0\n"+
		"veth12: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0\n",
		n(1000, 10000), n(500, 1000)))
}

func values(t *testing.T, c *Collector, at time.Time) map[string]float64 {
	t.Helper()
	out := map[string]float64{}
	for _, p := range c.Sample(at).Points {
		key := p.N
		for _, k := range []string{"device", "mount", "iface"} {
			if v, ok := p.L[k]; ok {
				key += "{" + v + "}"
			}
		}
		out[key] = p.V
	}
	return out
}

func TestSampleComputesRatesFromTheSecondCycle(t *testing.T) {
	h := newFakeHost(t)
	c := New(h.proc, h.sys)
	c.FS = func(path string) (FSStat, error) {
		return FSStat{Size: 1000, Free: 400, Avail: 300, Files: 100, FilesFree: 75}, nil
	}
	c.FailedUnits = func() ([]string, error) { return []string{"a.service", "b.mount"}, nil }
	t0 := time.Unix(1_700_000_000, 0)

	first := values(t, c, t0)
	if _, ok := first["cpu.usage_pct"]; ok {
		t.Fatal("no CPU rate without a previous reading")
	}
	want := map[string]float64{
		"cpu.count": 2, "load.1": 0.5, "load.15": 0.1,
		"mem.total_bytes": 1000 * 1024, "mem.available_bytes": 250 * 1024, "mem.used_pct": 75,
		"swap.used_bytes": 100 * 1024, "swap.used_pct": 25,
		"system.uptime_s": 3600.5, "proc.count": 2, "systemd.failed_units": 2,
		// df semantics: used / (used + available)
		"fs.used_pct{/}": 66.67, "fs.free_bytes{/}": 300, "fs.inodes_used_pct{/}": 25,
		"fs.used_pct{/mnt/my disk}": 66.67,
	}
	for k, v := range want {
		if first[k] != v {
			t.Errorf("%s = %v, want %v", k, first[k], v)
		}
	}
	for _, k := range []string{"fs.used_pct{/mnt/bind}", "fs.used_pct{/proc}", "fs.used_pct{/mnt/nfs}"} {
		if _, ok := first[k]; ok {
			t.Errorf("%s must not be collected", k)
		}
	}

	h.cycle(1)
	second := values(t, c, t0.Add(10*time.Second))
	// Δtotal = 30+10+50+5+5 = 100; idle+iowait Δ = 55 -> 45 % busy
	rates := map[string]float64{
		"cpu.usage_pct": 45, "cpu.iowait_pct": 5, "cpu.steal_pct": 5,
		"disk.read_bytes_ps{sda}": 2000 * 512 / 10, "disk.write_bytes_ps{sda}": 4000 * 512 / 10,
		"disk.util_pct{sda}":    50,
		"net.rx_bytes_ps{eth0}": 1000, "net.tx_bytes_ps{eth0}": 100,
	}
	for k, v := range rates {
		if second[k] != v {
			t.Errorf("%s = %v, want %v", k, second[k], v)
		}
	}
	for _, k := range []string{"disk.util_pct{sda1}", "disk.util_pct{loop0}", "net.rx_bytes_ps{lo}", "net.rx_bytes_ps{veth12}"} {
		if _, ok := second[k]; ok {
			t.Errorf("%s must be filtered out", k)
		}
	}
}

func TestCounterResetGivesZeroNotNegative(t *testing.T) {
	h := newFakeHost(t)
	c := New(h.proc, h.sys)
	c.FS = func(string) (FSStat, error) { return FSStat{}, os.ErrNotExist }
	c.FailedUnits = nil
	h.cycle(5)
	values(t, c, time.Unix(0, 0))
	h.cycle(0) // e.g. counters restarted
	v := values(t, c, time.Unix(10, 0))
	if v["net.rx_bytes_ps{eth0}"] != 0 || v["disk.read_bytes_ps{sda}"] != 0 {
		t.Fatalf("reset counters must give 0: %v", v)
	}
}

func TestTopProcessesAndOddNames(t *testing.T) {
	h := newFakeHost(t)
	c := New(h.proc, h.sys)
	c.TopProcesses(5, 0)
	h.write("proc/42/stat", "42 (my (odd) cmd) R 1 42 42 0 -1 4194560 100 0 0 0 110 0 0 0 20 0 1 0 10 100000 1000 18446744073709551615\n")
	byCPU, byMem := c.TopProcesses(5, 10*time.Second)
	if byCPU[0].Name != "my (odd) cmd" || byCPU[0].CPUPct != 10 {
		t.Fatalf("top cpu = %+v", byCPU[0])
	}
	if byMem[0].PID != 42 || byMem[0].RSSBytes != 1000*uint64(os.Getpagesize()) {
		t.Fatalf("top mem = %+v", byMem[0])
	}
}

func TestListeningSockets(t *testing.T) {
	h := newFakeHost(t)
	h.write("proc/net/tcp", ""+
		"  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"+
		"   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1111 1 0 100 0 0 10 0\n"+
		"   1: 0100007F:1F90 0100007F:D431 01 00000000:00000000 00:00000000 00000000     0        0 2222 1 0 100 0 0 10 0\n")
	h.write("proc/net/tcp6", ""+
		"  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"+
		"   0: 00000000000000000000000001000000:0050 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 3333 1 0 100 0 0 10 0\n")
	h.write("proc/net/udp", ""+
		"  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops\n"+
		"   0: 3500007F:0035 00000000:0000 07 00000000:00000000 00:00000000 00000000   101        0 4444 2 0 0\n"+
		"   1: 0A00000A:A1B2 08080808:0035 07 00000000:00000000 00:00000000 00000000   101        0 5555 2 0 0\n")
	if err := os.MkdirAll(filepath.Join(h.proc, "1", "fd"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("socket:[1111]", filepath.Join(h.proc, "1", "fd", "3")); err != nil {
		t.Fatal(err)
	}
	got := New(h.proc, h.sys).Listening()
	want := []string{"tcp 0.0.0.0:22 systemd", "udp 127.0.0.53:53 ", "tcp ::1:80 "}
	if len(got) != len(want) {
		t.Fatalf("got %+v", got)
	}
	for i, l := range got {
		if s := sprintf("%s %s:%d %s", l.Proto, l.Addr, l.Port, l.Process); s != want[i] {
			t.Errorf("listener %d = %q, want %q", i, s, want[i])
		}
	}
}

func TestParseFailedUnits(t *testing.T) {
	out := "nginx.service loaded failed failed A high performance web server\nbackup.timer loaded failed failed Nightly\n"
	got := ParseFailedUnits(out)
	if len(got) != 2 || got[0] != "nginx.service" || got[1] != "backup.timer" {
		t.Fatalf("got %v", got)
	}
	if len(ParseFailedUnits("")) != 0 {
		t.Fatal("empty output = no failed units")
	}
}

func TestInfoReadsOSRelease(t *testing.T) {
	h := newFakeHost(t)
	h.write("etc/os-release", "NAME=Debian\nPRETTY_NAME=\"Debian GNU/Linux 13 (trixie)\"\n")
	h.write("proc/sys/kernel/osrelease", "6.12.0-test\n")
	info := New(h.proc, h.sys).Info()
	if info.OS != "Debian GNU/Linux 13 (trixie)" || info.Kernel != "6.12.0-test" {
		t.Fatalf("info = %+v", info)
	}
}

func TestDisplayName(t *testing.T) {
	cases := []struct {
		comm string
		args []string
		want string
	}{
		{"kvm", []string{"/usr/bin/kvm", "-id", "101", "-name", "web01,debug-threads=on", "-m", "4096"}, "kvm 101 (web01)"},
		{"qemu-system-x86", []string{"qemu-system-x86_64", "-name", "guest=db,debug-threads=on"}, "qemu-system-x86 (db)"},
		{"pvedaemon worke", []string{"pvedaemon worker"}, "pvedaemon worker"},
		{"systemd-network", []string{"/usr/lib/systemd/systemd-networkd"}, "systemd-networkd"},
		{"xyOps Satellite", []string{"node", "/opt/sat/main.js"}, "xyOps Satellite"},
		{"ksmd", nil, "ksmd"}, // kernel thread: no command line
		{"sshd", []string{"sshd: /usr/sbin/sshd -D"}, "sshd"},
	}
	for _, tc := range cases {
		if got := displayName(tc.comm, tc.args); got != tc.want {
			t.Errorf("displayName(%q, %q) = %q, want %q", tc.comm, tc.args, got, tc.want)
		}
	}
}

func TestTopProcessesDescribeCommandAndUser(t *testing.T) {
	h := newFakeHost(t)
	h.write("etc/passwd", "root:x:0:0:root:/root:/bin/bash\nwww-data:x:33:33::/var/www:/usr/sbin/nologin\n")
	h.write("proc/42/stat", "42 (kvm) S 1 42 42 0 -1 4194560 100 0 0 0 10 0 0 0 20 0 1 0 10 100000 1000 18446744073709551615\n")
	h.write("proc/42/cmdline", "/usr/bin/kvm\x00-id\x00105\x00-name\x00mail,debug-threads=on\x00")
	h.write("proc/42/status", "Name:\tkvm\nUid:\t33\t33\t33\t33\n")
	h.write("proc/1/status", "Name:\tsystemd\nUid:\t1000\t0\t0\t0\n")
	_, byMem := New(h.proc, h.sys).TopProcesses(5, 0)
	if byMem[0].Name != "kvm 105 (mail)" || byMem[0].User != "www-data" {
		t.Fatalf("top = %+v", byMem[0])
	}
	if byMem[0].Cmd != "/usr/bin/kvm -id 105 -name mail,debug-threads=on" {
		t.Fatalf("cmd = %q", byMem[0].Cmd)
	}
	if byMem[1].User != "1000" || byMem[1].Cmd != "" { // unknown uid, no cmdline file
		t.Fatalf("second = %+v", byMem[1])
	}
}
