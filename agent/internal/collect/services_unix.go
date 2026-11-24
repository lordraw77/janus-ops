//go:build !windows

package collect

import (
	"bufio"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

// Service discovery: which well-known services run on the host, and the few settings of theirs
// that a monitoring template needs (port, data directory, listen ports…).
//
// Everything here is read only and fixed in code: the processes looked for, the files read and the
// keys kept are the catalog below. Nothing else of a configuration file leaves the host, and no
// secret (a password, a key, a connection string) is ever read into the result.

const (
	maxServices = 60
)

type serviceDef struct {
	id    string
	procs []string // kernel command names of the main process
	read  func(c *Collector, args []string) map[string]string
	// the service has one port: when no file or flag gives it, the lowest one its process listens on
	listenOwner bool
	// the process name is shared (java, master, beam.smp): its command line must contain this
	cmdHas string
}

var serviceCatalog = []serviceDef{
	{id: "postgresql", procs: []string{"postgres", "postmaster"}, read: readPostgres},
	{id: "httpd", procs: []string{"httpd", "apache2"}, read: readApache},
}

// file opens a path under the collector's root (the real root, except in tests).
func (c *Collector) file(path string) string { return filepath.Join(c.Root, path) }

// Services returns the catalog services that are running, each with its command line and the
// settings read from its configuration. The main process is the lowest pid of its name.
func (c *Collector) Services() []proto.Service {
	pids, err := c.pids()
	if err != nil {
		return []proto.Service{}
	}
	sort.Ints(pids)
	found := map[string]proto.Service{}
	var listening []proto.Listener
	for _, pid := range pids {
		st, ok := c.readStat(pid)
		if !ok {
			continue
		}
		var args []string
		for _, def := range serviceCatalog {
			if _, done := found[def.id]; done || !contains(def.procs, st.name) {
				continue
			}
			if args == nil {
				args = c.cmdline(pid)
			}
			cmd := strings.Join(args, " ")
			if def.cmdHas != "" && !strings.Contains(cmd, def.cmdHas) {
				continue
			}
			if len(cmd) > maxServiceCmd {
				cmd = cmd[:maxServiceCmd]
			}
			svc := proto.Service{Name: def.id, Process: st.name, Cmd: cmd, Config: map[string]string{}}
			if def.read != nil {
				for k, v := range def.read(c, args) {
					svc.Config[k] = v
				}
			}
			if def.listenOwner && svc.Config["port"] == "" {
				if listening == nil {
					listening = c.Listening()
				}
				if p := lowestPort(listening, st.name); p > 0 {
					svc.Config["port"] = strconv.Itoa(p)
				}
			}
			found[def.id] = svc
		}
	}
	out := make([]proto.Service, 0, len(found))
	for _, s := range found {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	if len(out) > maxServices {
		out = out[:maxServices]
	}
	return out
}

// argValue finds `flag value` or `flag=value` among the arguments.
func argValue(args []string, flag string) string {
	for i, a := range args {
		if a == flag && i+1 < len(args) {
			return args[i+1]
		}
		if strings.HasPrefix(a, flag+"=") {
			return strings.TrimPrefix(a, flag+"=")
		}
	}
	return ""
}

// settingValue finds `-c name=value` (PostgreSQL style) among the arguments.
func settingValue(args []string, name string) string {
	for i, a := range args {
		if a == "-c" && i+1 < len(args) && strings.HasPrefix(args[i+1], name+"=") {
			return strings.TrimPrefix(args[i+1], name+"=")
		}
		if strings.HasPrefix(a, "--"+strings.ReplaceAll(name, "_", "-")+"=") {
			return a[strings.IndexByte(a, '=')+1:]
		}
	}
	return ""
}

// ---------------------------------------------------------------- PostgreSQL

// Keys of postgresql.conf that are kept: none can hold a secret.
var postgresKeys = []string{
	"port", "listen_addresses", "max_connections", "unix_socket_directories", "data_directory",
}

func readPostgres(c *Collector, args []string) map[string]string {
	out := map[string]string{}
	data := argValue(args, "-D")
	if data == "" {
		data = argValue(args, "--pgdata")
	}
	conf := settingValue(args, "config_file")
	if conf == "" && data != "" {
		conf = filepath.Join(data, "postgresql.conf")
	}
	if data != "" {
		out["data_dir"] = data
	}
	if conf != "" {
		out["config_file"] = conf
		values := parseSettings(c.file(conf))
		for _, k := range postgresKeys {
			if v, ok := values[k]; ok && v != "" {
				out[k] = v
			}
		}
	}
	// a port given on the command line wins over the file
	if p := argValue(args, "-p"); p != "" {
		out["port"] = p
	}
	return out
}

// parseSettings reads `key = value  # comment` lines (postgresql.conf, my.cnf-like files).
// The last assignment of a key wins; quotes are removed.
func parseSettings(path string) map[string]string {
	out := map[string]string{}
	f, err := os.Open(path)
	if err != nil {
		return out
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		line := strings.TrimSpace(s.Text())
		if line == "" || line[0] == '#' {
			continue
		}
		key, rest, ok := strings.Cut(line, "=")
		if !ok {
			// "key value" form
			fields := strings.Fields(line)
			if len(fields) < 2 {
				continue
			}
			key, rest = fields[0], strings.Join(fields[1:], " ")
		}
		rest = strings.TrimSpace(rest)
		if strings.HasPrefix(rest, "'") {
			if end := strings.IndexByte(rest[1:], '\''); end >= 0 {
				rest = rest[1 : 1+end]
			}
		} else if i := strings.IndexByte(rest, '#'); i >= 0 {
			rest = strings.TrimSpace(rest[:i])
		}
		out[strings.TrimSpace(key)] = rest
	}
	return out
}

// ---------------------------------------------------------------- Apache httpd

var (
	apacheMain    = []string{"/etc/httpd/conf/httpd.conf", "/etc/apache2/apache2.conf"}
	apacheExtra   = []string{"/etc/apache2/ports.conf", "/etc/httpd/conf.d/*.conf", "/etc/apache2/conf-enabled/*.conf"}
	apacheListen  = regexp.MustCompile(`(?i)^\s*Listen\s+(\S+)(?:\s+(\S+))?`)
	apacheStatus  = regexp.MustCompile(`(?i)server-status|mod_status|status_module`)
	apacheComment = regexp.MustCompile(`(?i)^\s*#`)
)

func readApache(c *Collector, args []string) map[string]string {
	out := map[string]string{}
	main := argValue(args, "-f")
	if main == "" {
		for _, candidate := range apacheMain {
			if _, err := os.Stat(c.file(candidate)); err == nil {
				main = candidate
				break
			}
		}
	}
	var files []string
	if main != "" {
		out["config_file"] = main
		files = append(files, main)
	}
	for _, pattern := range apacheExtra {
		matches, _ := filepath.Glob(c.file(pattern))
		for _, m := range matches {
			files = append(files, strings.TrimPrefix(m, filepath.Clean(c.Root)))
		}
	}
	ports, https := map[int]bool{}, map[int]bool{}
	status := false
	for _, name := range files {
		f, err := os.Open(c.file(name))
		if err != nil {
			continue
		}
		s := bufio.NewScanner(f)
		for s.Scan() {
			line := s.Text()
			if apacheComment.MatchString(line) {
				continue
			}
			if apacheStatus.MatchString(line) {
				status = true
			}
			m := apacheListen.FindStringSubmatch(line)
			if m == nil {
				continue
			}
			spec := m[1]
			if i := strings.LastIndexByte(spec, ':'); i >= 0 {
				spec = spec[i+1:]
			}
			port, err := strconv.Atoi(spec)
			if err != nil || port < 1 || port > 65535 {
				continue
			}
			ports[port] = true
			if strings.EqualFold(m[2], "https") || strings.Contains(strings.ToLower(name), "ssl") || port == 443 {
				https[port] = true
			}
		}
		f.Close()
	}
	if len(ports) > 0 {
		out["listen_ports"] = joinPorts(ports)
		if len(https) > 0 {
			out["https_ports"] = joinPorts(https)
		}
		plain := map[int]bool{}
		for p := range ports {
			if !https[p] {
				plain[p] = true
			}
		}
		if len(plain) > 0 {
			out["http_ports"] = joinPorts(plain)
		}
	}
	if status {
		out["server_status"] = "yes"
	}
	return out
}

func joinPorts(set map[int]bool) string {
	list := make([]int, 0, len(set))
	for p := range set {
		list = append(list, p)
	}
	sort.Ints(list)
	parts := make([]string, len(list))
	for i, p := range list {
		parts[i] = strconv.Itoa(p)
	}
	return strings.Join(parts, ",")
}
