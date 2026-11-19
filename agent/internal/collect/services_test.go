package collect

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func (h *fakeHost) root(rel, content string) {
	path := filepath.Join(filepath.Dir(h.proc), "root", rel)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		h.t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		h.t.Fatal(err)
	}
}

func (h *fakeHost) process(pid, comm string, args ...string) {
	h.write("proc/"+pid+"/stat", pid+" ("+comm+") S 1 "+pid+" "+pid+" 0 -1 4194560 100 0 0 0 1 1 0 0 20 0 1 0 10 100000 100 18446744073709551615\n")
	h.write("proc/"+pid+"/cmdline", strings.Join(args, "\x00")+"\x00")
}

func services(h *fakeHost) *Collector {
	c := New(h.proc, h.sys)
	c.Root = filepath.Join(filepath.Dir(h.proc), "root")
	return c
}

func TestPostgresSettingsComeFromTheCommandLineAndTheConfig(t *testing.T) {
	h := newFakeHost(t)
	h.process("700", "postgres", "/usr/lib/postgresql/16/bin/postgres", "-D", "/var/lib/postgresql/16/main",
		"-c", "config_file=/etc/postgresql/16/main/postgresql.conf")
	h.process("701", "postgres", "postgres: checkpointer") // a child: the lowest pid is the main one
	h.root("etc/postgresql/16/main/postgresql.conf", ""+
		"# comment\n"+
		"data_directory = '/var/lib/postgresql/16/main'\t# use data in another directory\n"+
		"port = 5433\n"+
		"listen_addresses = 'localhost,10.0.0.5'\n"+
		"max_connections = 200\n"+
		"password_encryption = scram-sha-256\n"+
		"ssl_key_file = '/etc/ssl/private/ssl-cert-snakeoil.key'\n")
	got := services(h).Services()
	if len(got) != 1 || got[0].Name != "postgresql" {
		t.Fatalf("services = %+v", got)
	}
	cfg := got[0].Config
	want := map[string]string{
		"port": "5433", "listen_addresses": "localhost,10.0.0.5", "max_connections": "200",
		"data_directory": "/var/lib/postgresql/16/main", "data_dir": "/var/lib/postgresql/16/main",
		"config_file": "/etc/postgresql/16/main/postgresql.conf",
	}
	for k, v := range want {
		if cfg[k] != v {
			t.Errorf("config[%s] = %q, want %q", k, cfg[k], v)
		}
	}
	// only the catalog keys leave the host
	if _, leaked := cfg["ssl_key_file"]; leaked {
		t.Error("ssl_key_file must not be reported")
	}
	if _, leaked := cfg["password_encryption"]; leaked {
		t.Error("password_encryption must not be reported")
	}
	if got[0].Process != "postgres" || !strings.Contains(got[0].Cmd, "-D /var/lib/postgresql/16/main") {
		t.Errorf("service = %+v", got[0])
	}
}

func TestPostgresPortOnTheCommandLineWinsAndTheDefaultConfigIsUnderTheDataDir(t *testing.T) {
	h := newFakeHost(t)
	h.process("800", "postmaster", "/usr/bin/postmaster", "-D", "/var/lib/pgsql/data", "-p", "5444")
	h.root("var/lib/pgsql/data/postgresql.conf", "port = 5432\n")
	got := services(h).Services()
	if len(got) != 1 || got[0].Config["port"] != "5444" || got[0].Config["config_file"] != "/var/lib/pgsql/data/postgresql.conf" {
		t.Fatalf("services = %+v", got)
	}
}

func TestApacheListenPortsAndStatus(t *testing.T) {
	h := newFakeHost(t)
	h.process("900", "httpd", "/usr/sbin/httpd", "-DFOREGROUND")
	h.root("etc/httpd/conf/httpd.conf", ""+
		"# Listen 9999\n"+
		"Listen 80\n"+
		"Listen 10.0.0.5:8080\n"+
		"LoadModule status_module modules/mod_status.so\n")
	h.root("etc/httpd/conf.d/ssl.conf", "Listen 443 https\n")
	got := services(h).Services()
	if len(got) != 1 || got[0].Name != "httpd" {
		t.Fatalf("services = %+v", got)
	}
	cfg := got[0].Config
	if cfg["listen_ports"] != "80,443,8080" || cfg["http_ports"] != "80,8080" || cfg["https_ports"] != "443" {
		t.Errorf("config = %+v", cfg)
	}
	if cfg["server_status"] != "yes" || cfg["config_file"] != "/etc/httpd/conf/httpd.conf" {
		t.Errorf("config = %+v", cfg)
	}
}

func TestDebianApacheUsesPortsConf(t *testing.T) {
	h := newFakeHost(t)
	h.process("950", "apache2", "/usr/sbin/apache2", "-k", "start")
	h.root("etc/apache2/apache2.conf", "ServerRoot /etc/apache2\n")
	h.root("etc/apache2/ports.conf", "Listen 80\n<IfModule ssl_module>\n\tListen 443\n</IfModule>\n")
	got := services(h).Services()
	if len(got) != 1 || got[0].Config["listen_ports"] != "80,443" || got[0].Config["https_ports"] != "443" {
		t.Fatalf("services = %+v", got)
	}
}

func TestNoCatalogServiceMeansAnEmptyList(t *testing.T) {
	h := newFakeHost(t)
	got := services(h).Services()
	if got == nil || len(got) != 0 {
		t.Fatalf("services = %#v", got)
	}
}

func TestFactsCarryUnitsAndServices(t *testing.T) {
	h := newFakeHost(t)
	h.process("700", "postgres", "/usr/bin/postgres", "-D", "/d")
	c := services(h)
	c.RunningUnits = func() ([]string, error) { return []string{"postgresql.service", "sshd.service"}, nil }
	c.FailedUnits = func() ([]string, error) { return nil, nil }
	d := c.Facts(0)
	if len(d.Units) != 2 || len(d.Services) != 1 {
		t.Fatalf("facts = %+v", d)
	}
}
