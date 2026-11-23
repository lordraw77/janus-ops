//go:build !windows

package collect

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// A service of the catalog that is described by data: the processes that identify it, the
// configuration files to read and, for each, the keys to keep. Nothing outside `keys` leaves the
// host (a password, token or key is never listed there), and a kept value that looks like a URL
// with credentials is dropped anyway.

type fileFormat int

const (
	fmtKV        fileFormat = iota // key = value, or key value
	fmtINI                         // [section] key = value: reported as section.key
	fmtDirective                   // nginx / HAProxy: `directive args;`
	fmtYAML                        // nested maps: reported as a.b.c
	fmtJSON                        // nested objects: reported as a.b.c
)

type confFile struct {
	paths  []string          // globs under the root; the first one that exists is the config_file
	format fileFormat        //
	keys   map[string]string // key in the file (lower case) -> key reported
}

type confSpec struct {
	id       string
	procs    []string          // kernel command names (15 characters at most)
	files    []confFile        //
	args     map[string]string // command-line flag -> key reported
	portKeys []string          // reported keys that hold listen addresses: listen_ports come from them
	// the service has one port and no file says which: take the lowest one the process listens on
	listenOwner bool
	cmdHas      string // see serviceDef.cmdHas
}

const (
	maxConfValue  = 256
	maxFileBytes  = 1 << 20
	maxListValues = 16
)

var credentialURL = regexp.MustCompile(`://[^/\s]*@`)

func safeValue(v string) (string, bool) {
	v = strings.TrimSpace(v)
	if v == "" || credentialURL.MatchString(v) {
		return "", false
	}
	if len(v) > maxConfValue {
		v = v[:maxConfValue]
	}
	return v, true
}

func unquote(s string) string {
	s = strings.TrimSpace(s)
	if len(s) >= 2 && (s[0] == '"' || s[0] == '\'') && s[len(s)-1] == s[0] {
		return s[1 : len(s)-1]
	}
	return s
}

func stripComment(s string) string {
	inQuote := byte(0)
	for i := 0; i < len(s); i++ {
		switch ch := s[i]; {
		case inQuote != 0:
			if ch == inQuote {
				inQuote = 0
			}
		case ch == '"' || ch == '\'':
			inQuote = ch
		case ch == '#' && (i == 0 || s[i-1] == ' ' || s[i-1] == '\t'):
			return strings.TrimSpace(s[:i])
		}
	}
	return strings.TrimSpace(s)
}

func readFileLines(path string) []string {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	var lines []string
	s := bufio.NewScanner(f)
	s.Buffer(make([]byte, 64*1024), maxFileBytes)
	for s.Scan() {
		lines = append(lines, s.Text())
		if len(lines) > 20000 {
			break
		}
	}
	return lines
}

// confValues maps a lower-case key to every value it was given, in file order.
type confValues map[string][]string

func (v confValues) add(k, val string) { v[strings.ToLower(k)] = append(v[strings.ToLower(k)], val) }

func parseKVFile(path string) confValues {
	out := confValues{}
	for _, raw := range readFileLines(path) {
		line := stripComment(raw)
		if line == "" {
			continue
		}
		key, rest, ok := strings.Cut(line, "=")
		if !ok {
			fields := strings.Fields(line)
			if len(fields) < 2 {
				continue
			}
			key, rest = fields[0], strings.Join(fields[1:], " ")
		}
		out.add(strings.TrimSpace(key), unquote(rest))
	}
	return out
}

func parseINIFile(path string) confValues {
	out := confValues{}
	section := ""
	for _, raw := range readFileLines(path) {
		line := strings.TrimSpace(raw)
		if line == "" || line[0] == '#' || line[0] == ';' {
			continue
		}
		if line[0] == '[' && strings.HasSuffix(line, "]") {
			section = strings.ToLower(strings.TrimSpace(line[1 : len(line)-1]))
			continue
		}
		key, rest, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		if i := strings.Index(rest, " #"); i >= 0 {
			rest = rest[:i]
		}
		if i := strings.Index(rest, " ;"); i >= 0 {
			rest = rest[:i]
		}
		out.add(section+"."+strings.TrimSpace(key), unquote(rest))
	}
	return out
}

func parseDirectiveFile(path string) confValues {
	out := confValues{}
	for _, raw := range readFileLines(path) {
		line := strings.TrimSuffix(stripComment(raw), ";")
		line = strings.TrimSpace(line)
		if line == "" || line == "}" || strings.HasSuffix(line, "{") {
			continue
		}
		fields := strings.Fields(line)
		out.add(fields[0], strings.Join(fields[1:], " "))
	}
	return out
}

// parseYAMLFile reads the scalars of a block-style YAML file (nested maps, lists of scalars).
// Flow style, anchors and multi-line scalars are not understood: those lines are skipped.
func parseYAMLFile(path string) confValues {
	out := confValues{}
	type level struct {
		indent int
		key    string
	}
	var stack []level
	listKey := ""
	for _, raw := range readFileLines(path) {
		line := strings.TrimRight(raw, " \t")
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || trimmed[0] == '#' || trimmed == "---" {
			continue
		}
		indent := len(line) - len(strings.TrimLeft(line, " "))
		if strings.HasPrefix(trimmed, "- ") {
			if listKey != "" {
				out.add(listKey, unquote(stripComment(strings.TrimPrefix(trimmed, "- "))))
			}
			continue
		}
		key, rest, ok := strings.Cut(trimmed, ":")
		if !ok {
			continue
		}
		for len(stack) > 0 && stack[len(stack)-1].indent >= indent {
			stack = stack[:len(stack)-1]
		}
		path := key
		if len(stack) > 0 {
			path = stack[len(stack)-1].key + "." + key
		}
		rest = stripComment(rest)
		if rest == "" {
			stack = append(stack, level{indent, path})
			listKey = path
			continue
		}
		listKey = ""
		if strings.HasPrefix(rest, "[") && strings.HasSuffix(rest, "]") {
			for _, item := range strings.Split(rest[1:len(rest)-1], ",") {
				out.add(path, unquote(item))
			}
			continue
		}
		out.add(path, unquote(rest))
	}
	return out
}

func flattenJSON(prefix string, v any, out confValues) {
	switch t := v.(type) {
	case map[string]any:
		for k, x := range t {
			p := k
			if prefix != "" {
				p = prefix + "." + k
			}
			flattenJSON(p, x, out)
		}
	case []any:
		for _, x := range t {
			flattenJSON(prefix, x, out)
		}
	case string:
		out.add(prefix, t)
	case float64:
		out.add(prefix, strconv.FormatFloat(t, 'f', -1, 64))
	case bool:
		out.add(prefix, strconv.FormatBool(t))
	}
}

func parseJSONFile(path string) confValues {
	out := confValues{}
	raw, err := os.ReadFile(path)
	if err != nil || len(raw) > maxFileBytes {
		return out
	}
	var doc any
	if json.Unmarshal(raw, &doc) == nil {
		flattenJSON("", doc, out)
	}
	return out
}

func parseConf(format fileFormat, path string) confValues {
	switch format {
	case fmtINI:
		return parseINIFile(path)
	case fmtDirective:
		return parseDirectiveFile(path)
	case fmtYAML:
		return parseYAMLFile(path)
	case fmtJSON:
		return parseJSONFile(path)
	}
	return parseKVFile(path)
}

// portOf reads the port of `80`, `*:80`, `[::]:8080`, `10.0.0.5:443 ssl` or `:6379`.
func portOf(spec string) (int, bool) {
	fields := strings.Fields(spec)
	if len(fields) == 0 {
		return 0, false
	}
	s := fields[0]
	if i := strings.LastIndexByte(s, ':'); i >= 0 {
		s = s[i+1:]
	}
	p, err := strconv.Atoi(s)
	return p, err == nil && p >= 1 && p <= 65535
}

func uniqueJoin(list []string) string {
	seen := map[string]bool{}
	var out []string
	for _, v := range list {
		if v = strings.TrimSpace(v); v != "" && !seen[v] {
			seen[v] = true
			out = append(out, v)
		}
		if len(out) >= maxListValues {
			break
		}
	}
	return strings.Join(out, ",")
}

// reader turns a spec into the function the catalog calls with the command line of the process.
func (s confSpec) reader() func(c *Collector, args []string) map[string]string {
	return func(c *Collector, args []string) map[string]string {
		out := map[string]string{}
		for flag, key := range s.args {
			if v, ok := safeValue(argValue(args, flag)); ok {
				out[key] = v
			}
		}
		raw := map[string][]string{} // reported key -> every value, for the port lists
		for _, f := range s.files {
			var matches []string
			for _, pattern := range f.paths {
				found, _ := filepath.Glob(c.file(pattern))
				sort.Strings(found)
				matches = append(matches, found...)
			}
			for i, m := range matches {
				rel := "/" + strings.TrimPrefix(strings.TrimPrefix(m, filepath.Clean(c.Root)), "/")
				if _, set := out["config_file"]; !set && i == 0 {
					out["config_file"] = rel
				}
				for key, vals := range parseConf(f.format, m) {
					reported, keep := f.keys[key]
					if !keep {
						continue
					}
					raw[reported] = append(raw[reported], vals...)
				}
			}
		}
		for key, vals := range raw {
			if _, fromArgs := out[key]; fromArgs {
				continue // the command line wins over the file
			}
			safe := make([]string, 0, len(vals))
			for _, v := range vals {
				if v, ok := safeValue(v); ok {
					safe = append(safe, v)
				}
			}
			if len(safe) == 0 {
				continue
			}
			if len(safe) == 1 {
				out[key] = safe[0]
			} else {
				out[key] = uniqueJoin(safe)
			}
		}
		listen := map[int]bool{}
		secure := map[int]bool{}
		for _, key := range s.portKeys {
			for _, v := range strings.Split(strings.Join(raw[key], "\n"), "\n") {
				if p, ok := portOf(v); ok {
					listen[p] = true
					if p == 443 || p == 8443 || strings.Contains(" "+strings.ToLower(v)+" ", " ssl ") {
						secure[p] = true
					}
				}
			}
		}
		if len(listen) > 0 {
			out["listen_ports"] = joinPorts(listen)
			plain := map[int]bool{}
			for p := range listen {
				if !secure[p] {
					plain[p] = true
				}
			}
			if len(secure) > 0 {
				out["https_ports"] = joinPorts(secure)
			}
			if len(plain) > 0 {
				out["http_ports"] = joinPorts(plain)
			}
		}
		return out
	}
}

func (s confSpec) def() serviceDef {
	return serviceDef{id: s.id, procs: s.procs, read: s.reader(), listenOwner: s.listenOwner, cmdHas: s.cmdHas}
}

func keys(pairs ...string) map[string]string {
	if len(pairs)%2 != 0 {
		panic(fmt.Sprintf("keys: odd number of arguments %v", pairs))
	}
	m := map[string]string{}
	for i := 0; i < len(pairs); i += 2 {
		m[pairs[i]] = pairs[i+1]
	}
	return m
}

// configCatalog: what the agent reads of each service. Process names are the kernel's (comm).
var configCatalog = []confSpec{
	{id: "nginx", procs: []string{"nginx"}, portKeys: []string{"listen"},
		files: []confFile{
			{paths: []string{"/etc/nginx/nginx.conf"}, format: fmtDirective, keys: keys("listen", "listen", "user", "user", "worker_processes", "worker_processes")},
			{paths: []string{"/etc/nginx/conf.d/*.conf", "/etc/nginx/sites-enabled/*", "/etc/nginx/default.d/*.conf"}, format: fmtDirective, keys: keys("listen", "listen", "stub_status", "stub_status")},
		}, args: keys("-c", "config_file")},
	// Nginx Proxy Manager: a Node.js backend (the abort flag is in its command line, installed or in a
	// container) in front of OpenResty. Reported: the ports its nginx listens on (80, 443 and the admin
	// UI on 81) and the domains of its proxy, redirection and dead hosts. The database settings of its
	// environment and the certificates are never read.
	{id: "nginx-proxy-manager", procs: []string{"node"}, cmdHas: "--abort_on_uncaught_exception", portKeys: []string{"listen"},
		files: []confFile{
			{paths: []string{"/etc/nginx/conf.d/production.conf"}, format: fmtDirective, keys: keys("listen", "listen")},
			{paths: []string{"/data/nginx/proxy_host/*.conf", "/data/nginx/redirection_host/*.conf", "/data/nginx/dead_host/*.conf"}, format: fmtDirective,
				keys: keys("listen", "listen", "server_name", "domains")},
		}},
	{id: "haproxy", procs: []string{"haproxy"}, portKeys: []string{"bind"},
		files: []confFile{{paths: []string{"/etc/haproxy/haproxy.cfg"}, format: fmtDirective, keys: keys("bind", "bind", "stats", "stats")}},
		args:  keys("-f", "config_file")},
	{id: "mysql", procs: []string{"mysqld"}, listenOwner: true,
		files: []confFile{{paths: []string{"/etc/mysql/my.cnf", "/etc/my.cnf", "/etc/mysql/mysql.conf.d/*.cnf", "/etc/mysql/conf.d/*.cnf", "/etc/my.cnf.d/*.cnf"}, format: fmtINI,
			keys: keys("mysqld.port", "port", "mysqld.bind-address", "bind_address", "mysqld.datadir", "data_dir", "mysqld.socket", "socket", "mysqld.max_connections", "max_connections")}},
		args: keys("--port", "port", "--datadir", "data_dir", "--defaults-file", "config_file")},
	{id: "mariadb", procs: []string{"mariadbd"}, listenOwner: true,
		files: []confFile{{paths: []string{"/etc/mysql/mariadb.conf.d/*.cnf", "/etc/mysql/my.cnf", "/etc/my.cnf", "/etc/my.cnf.d/*.cnf"}, format: fmtINI,
			keys: keys("mysqld.port", "port", "mysqld.bind-address", "bind_address", "mysqld.datadir", "data_dir", "mysqld.socket", "socket", "mysqld.max_connections", "max_connections")}},
		args: keys("--port", "port", "--datadir", "data_dir")},
	{id: "redis", procs: []string{"redis-server"}, listenOwner: true,
		files: []confFile{{paths: []string{"/etc/redis/redis.conf", "/etc/redis.conf"}, format: fmtKV,
			keys: keys("port", "port", "bind", "bind_address", "dir", "data_dir", "maxmemory", "maxmemory", "appendonly", "appendonly", "protected-mode", "protected_mode")}}},
	{id: "valkey", procs: []string{"valkey-server"}, listenOwner: true,
		files: []confFile{{paths: []string{"/etc/valkey/valkey.conf"}, format: fmtKV,
			keys: keys("port", "port", "bind", "bind_address", "dir", "data_dir")}}},
	{id: "mongodb", procs: []string{"mongod"}, listenOwner: true,
		files: []confFile{{paths: []string{"/etc/mongod.conf"}, format: fmtYAML,
			keys: keys("net.port", "port", "net.bindip", "bind_address", "storage.dbpath", "data_dir", "security.authorization", "authorization")}},
		args: keys("--port", "port", "--dbpath", "data_dir", "--config", "config_file")},
	{id: "memcached", procs: []string{"memcached"}, listenOwner: true,
		files: []confFile{{paths: []string{"/etc/memcached.conf", "/etc/sysconfig/memcached"}, format: fmtKV, keys: keys("-p", "port", "port", "port", "-l", "bind_address")}},
		args:  keys("-p", "port", "-l", "bind_address")},
	{id: "elasticsearch", procs: []string{"java"}, cmdHas: "org.elasticsearch",
		files: []confFile{{paths: []string{"/etc/elasticsearch/elasticsearch.yml"}, format: fmtYAML,
			keys: keys("http.port", "port", "network.host", "bind_address", "path.data", "data_dir", "cluster.name", "cluster_name", "xpack.security.enabled", "security")}}},
	{id: "rabbitmq", procs: []string{"beam.smp"}, cmdHas: "rabbit",
		files: []confFile{{paths: []string{"/etc/rabbitmq/rabbitmq.conf"}, format: fmtKV,
			keys: keys("listeners.tcp.default", "port", "management.tcp.port", "management_port", "management.listener.port", "management_port")}}},
	{id: "grafana", listenOwner: true, procs: []string{"grafana", "grafana-server"},
		files: []confFile{{paths: []string{"/etc/grafana/grafana.ini"}, format: fmtINI,
			keys: keys("server.http_port", "port", "server.http_addr", "bind_address", "server.domain", "domain", "server.protocol", "protocol", "paths.data", "data_dir")}},
		args: keys("-config", "config_file", "--config", "config_file")},
	{id: "prometheus", listenOwner: true, procs: []string{"prometheus"},
		args: keys("--web.listen-address", "listen_address", "--storage.tsdb.path", "data_dir", "--config.file", "config_file", "--storage.tsdb.retention.time", "retention")},
	{id: "docker", procs: []string{"dockerd"},
		files: []confFile{{paths: []string{"/etc/docker/daemon.json"}, format: fmtJSON,
			keys: keys("data-root", "data_dir", "storage-driver", "storage_driver", "log-driver", "log_driver", "hosts", "hosts", "live-restore", "live_restore")}}},
	{id: "sshd", procs: []string{"sshd"}, listenOwner: true, portKeys: []string{"port"},
		files: []confFile{{paths: []string{"/etc/ssh/sshd_config", "/etc/ssh/sshd_config.d/*.conf"}, format: fmtKV,
			keys: keys("port", "port", "permitrootlogin", "permit_root_login", "passwordauthentication", "password_authentication", "pubkeyauthentication", "pubkey_authentication", "listenaddress", "bind_address")}},
		args: keys("-f", "config_file")},
	{id: "postfix", procs: []string{"master"}, cmdHas: "postfix",
		files: []confFile{{paths: []string{"/etc/postfix/main.cf"}, format: fmtKV,
			keys: keys("myhostname", "hostname", "mydomain", "domain", "inet_interfaces", "bind_address", "inet_protocols", "protocols", "relayhost", "relay_host")}}},
	{id: "dnsmasq", procs: []string{"dnsmasq"},
		files: []confFile{{paths: []string{"/etc/dnsmasq.conf", "/etc/dnsmasq.d/*.conf"}, format: fmtKV,
			keys: keys("port", "port", "interface", "interface", "listen-address", "bind_address", "dhcp-range", "dhcp_range")}}},
	{id: "mosquitto", listenOwner: true, procs: []string{"mosquitto"},
		files: []confFile{{paths: []string{"/etc/mosquitto/mosquitto.conf", "/etc/mosquitto/conf.d/*.conf"}, format: fmtKV,
			keys: keys("listener", "port", "persistence_location", "data_dir", "allow_anonymous", "allow_anonymous")}},
		args: keys("-c", "config_file", "-p", "port")},
	{id: "influxdb", listenOwner: true, procs: []string{"influxd"},
		files: []confFile{{paths: []string{"/etc/influxdb/config.yml", "/etc/influxdb/config.toml"}, format: fmtKV,
			keys: keys("http-bind-address", "listen_address", "bolt-path", "data_dir", "engine-path", "data_dir")}}},
}

func init() {
	for _, spec := range configCatalog {
		serviceCatalog = append(serviceCatalog, spec.def())
	}
}
