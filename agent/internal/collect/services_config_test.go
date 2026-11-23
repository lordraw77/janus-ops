//go:build !windows

package collect

import "testing"

func TestNginxListenPortsAcrossFilesAndNoSecrets(t *testing.T) {
	h := newFakeHost(t)
	h.process("100", "nginx", "nginx: master process /usr/sbin/nginx")
	h.root("etc/nginx/nginx.conf", "user www-data;\nworker_processes auto;\nhttp {\n  include /etc/nginx/conf.d/*.conf;\n}\n")
	h.root("etc/nginx/conf.d/site.conf", ""+
		"server {\n  listen 80;\n  listen [::]:80;\n  listen 443 ssl;\n  ssl_certificate_key /etc/ssl/private/x.key;\n"+
		"  proxy_pass http://user:hunter2@backend;\n  location /nginx_status { stub_status; }\n}\n"+
		"# listen 9999;\n")
	got := services(h).Services()
	if len(got) != 1 || got[0].Name != "nginx" {
		t.Fatalf("services = %+v", got)
	}
	cfg := got[0].Config
	if cfg["listen_ports"] != "80,443" || cfg["http_ports"] != "80" || cfg["https_ports"] != "443" {
		t.Errorf("config = %+v", cfg)
	}
	if cfg["config_file"] != "/etc/nginx/nginx.conf" || cfg["user"] != "www-data" {
		t.Errorf("config = %+v", cfg)
	}
	for k, v := range cfg {
		if k == "ssl_certificate_key" || v == "http://user:hunter2@backend" {
			t.Errorf("leaked %s=%s", k, v)
		}
	}
}

func TestMysqlReadsTheMysqldSectionOnly(t *testing.T) {
	h := newFakeHost(t)
	h.process("200", "mysqld", "/usr/sbin/mysqld")
	h.root("etc/mysql/my.cnf", "[client]\nport = 1111\npassword = secret\n[mysqld]\nport = 3307\nbind-address = 0.0.0.0 # all\ndatadir=/var/lib/mysql\n")
	got := services(h).Services()
	if len(got) != 1 {
		t.Fatalf("services = %+v", got)
	}
	cfg := got[0].Config
	if cfg["port"] != "3307" || cfg["bind_address"] != "0.0.0.0" || cfg["data_dir"] != "/var/lib/mysql" {
		t.Errorf("config = %+v", cfg)
	}
	if _, leaked := cfg["password"]; leaked {
		t.Error("password leaked")
	}
}

func TestRedisPortFileAndTheDefaultCommandLineWins(t *testing.T) {
	h := newFakeHost(t)
	h.process("300", "redis-server", "/usr/bin/redis-server", "*:6379")
	h.root("etc/redis/redis.conf", "bind 127.0.0.1 -::1\nport 6380\nrequirepass hunter2\ndir /var/lib/redis\n")
	cfg := services(h).Services()[0].Config
	if cfg["port"] != "6380" || cfg["data_dir"] != "/var/lib/redis" || cfg["bind_address"] != "127.0.0.1 -::1" {
		t.Errorf("config = %+v", cfg)
	}
	if _, leaked := cfg["requirepass"]; leaked {
		t.Error("requirepass leaked")
	}
}

func TestMongoYamlNestedKeys(t *testing.T) {
	h := newFakeHost(t)
	h.process("400", "mongod", "/usr/bin/mongod", "--config", "/etc/mongod.conf")
	h.root("etc/mongod.conf", "storage:\n  dbPath: /var/lib/mongodb\nnet:\n  port: 27018\n  bindIp: 127.0.0.1\nsecurity:\n  authorization: enabled\n")
	cfg := services(h).Services()[0].Config
	if cfg["port"] != "27018" || cfg["data_dir"] != "/var/lib/mongodb" || cfg["bind_address"] != "127.0.0.1" || cfg["authorization"] != "enabled" {
		t.Errorf("config = %+v", cfg)
	}
}

func TestSshdSecuritySettings(t *testing.T) {
	h := newFakeHost(t)
	h.process("500", "sshd", "sshd: /usr/sbin/sshd -D")
	h.root("etc/ssh/sshd_config", "Port 2222\nPermitRootLogin yes\nPasswordAuthentication no\n")
	cfg := services(h).Services()[0].Config
	if cfg["listen_ports"] != "2222" || cfg["permit_root_login"] != "yes" || cfg["password_authentication"] != "no" {
		t.Errorf("config = %+v", cfg)
	}
}

func TestSharedProcessNamesNeedTheirCommandLine(t *testing.T) {
	h := newFakeHost(t)
	h.process("600", "java", "java", "-jar", "app.jar")
	h.process("601", "master", "/usr/lib/other/master")
	if got := services(h).Services(); len(got) != 0 {
		t.Fatalf("services = %+v", got)
	}
	h2 := newFakeHost(t)
	h2.process("602", "java", "java", "-cp", "lib", "org.elasticsearch.bootstrap.Elasticsearch")
	h2.root("etc/elasticsearch/elasticsearch.yml", "cluster.name: prod\nhttp.port: 9201\n")
	got := services(h2).Services()
	if len(got) != 1 || got[0].Name != "elasticsearch" || got[0].Config["port"] != "9201" {
		t.Fatalf("services = %+v", got)
	}
}

func TestDockerDaemonJSONAndPrometheusFlags(t *testing.T) {
	h := newFakeHost(t)
	h.process("700", "dockerd", "/usr/bin/dockerd")
	h.process("701", "prometheus", "/usr/bin/prometheus", "--web.listen-address=:9091", "--storage.tsdb.path=/data")
	h.root("etc/docker/daemon.json", `{"data-root":"/srv/docker","log-driver":"json-file","registry-mirrors":["https://u:p@m"]}`)
	byName := map[string]map[string]string{}
	for _, s := range services(h).Services() {
		byName[s.Name] = s.Config
	}
	if byName["docker"]["data_dir"] != "/srv/docker" || byName["docker"]["log_driver"] != "json-file" {
		t.Errorf("docker = %+v", byName["docker"])
	}
	if byName["prometheus"]["listen_address"] != ":9091" || byName["prometheus"]["data_dir"] != "/data" {
		t.Errorf("prometheus = %+v", byName["prometheus"])
	}
}

func TestNginxProxyManagerReportsItsPortsAndDomains(t *testing.T) {
	h := newFakeHost(t)
	h.process("113", "node", "/usr/bin/node", "index.js", "--abort_on_uncaught_exception", "--max_old_space_size=250")
	h.process("114", "node", "/usr/bin/node", "other.js") // another Node.js program
	h.root("etc/nginx/conf.d/production.conf", "server {\n  listen 81 default;\n}\n")
	h.root("data/nginx/proxy_host/1.conf", "server {\n  listen 80;\n  listen 443 ssl;\n  server_name app.example.org www.example.org;\n  ssl_certificate_key /data/custom_ssl/k.pem;\n}\n")
	h.root("data/nginx/proxy_host/2.conf", "server {\n  listen 80;\n  server_name wiki.example.org;\n}\n")
	got := services(h).Services()
	if len(got) != 1 || got[0].Name != "nginx-proxy-manager" {
		t.Fatalf("services = %+v", got)
	}
	cfg := got[0].Config
	if cfg["listen_ports"] != "80,81,443" || cfg["https_ports"] != "443" {
		t.Errorf("config = %+v", cfg)
	}
	if cfg["domains"] != "app.example.org www.example.org,wiki.example.org" {
		t.Errorf("domains = %q", cfg["domains"])
	}
	if _, leaked := cfg["ssl_certificate_key"]; leaked {
		t.Error("certificate key path leaked")
	}
}
