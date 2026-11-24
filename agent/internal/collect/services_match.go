package collect

import (
	"sort"
	"strconv"
	"strings"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

const maxServiceCmd = 512

// lowestPort is the lowest TCP port a process of that name listens on, or 0.
func lowestPort(listening []proto.Listener, process string) int {
	lowest := 0
	for _, l := range listening {
		if l.Proto == "tcp" && l.Process == process && (lowest == 0 || l.Port < lowest) {
			lowest = l.Port
		}
	}
	return lowest
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// Service discovery on Windows. The same product ids as the Linux catalog (so the templates and the
// "Services found" list work unchanged), found by the name of the running process. What Linux
// also reads from configuration files is not read here: the port is the lowest TCP port the
// process listens on, and the command is the path of its executable. Products that run inside a
// shared host process (Java, Erlang, Node) are left out: the name alone does not tell them apart.
var windowsCatalog = []struct {
	id    string
	procs []string // lower case, without ".exe"
}{
	{"postgresql", []string{"postgres"}},
	{"httpd", []string{"httpd", "apache2"}},
	{"nginx", []string{"nginx"}},
	{"haproxy", []string{"haproxy"}},
	{"mysql", []string{"mysqld"}},
	{"mariadb", []string{"mariadbd"}},
	{"redis", []string{"redis-server"}},
	{"valkey", []string{"valkey-server"}},
	{"mongodb", []string{"mongod"}},
	{"memcached", []string{"memcached"}},
	{"grafana", []string{"grafana", "grafana-server"}},
	{"prometheus", []string{"prometheus"}},
	{"docker", []string{"dockerd"}},
	{"sshd", []string{"sshd"}},
	{"mosquitto", []string{"mosquitto"}},
	{"influxdb", []string{"influxd"}},
}

// windowsServices matches the running processes (pid to executable name) against the catalog.
// path gives the full path of a pid's executable ("" when it cannot be read).
func windowsServices(procs map[uint32]string, listening []proto.Listener, path func(pid uint32) string) []proto.Service {
	pids := make([]uint32, 0, len(procs))
	for pid := range procs {
		pids = append(pids, pid)
	}
	sort.Slice(pids, func(i, j int) bool { return pids[i] < pids[j] }) // the main process is the lowest pid
	found := map[string]proto.Service{}
	for _, pid := range pids {
		name := processName(procs[pid])
		for _, def := range windowsCatalog {
			if _, done := found[def.id]; done || !contains(def.procs, strings.ToLower(name)) {
				continue
			}
			svc := proto.Service{Name: def.id, Process: name, Config: map[string]string{}}
			if p := path(pid); p != "" {
				if len(p) > maxServiceCmd {
					p = p[:maxServiceCmd]
				}
				svc.Cmd = p
			}
			if port := lowestPort(listening, name); port > 0 {
				svc.Config["port"] = strconv.Itoa(port)
			}
			found[def.id] = svc
		}
	}
	out := make([]proto.Service, 0, len(found))
	for _, s := range found {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}
