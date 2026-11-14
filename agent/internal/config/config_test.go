package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const valid = `# written by Janus-Ops
server_url: https://janus.example.com:8443/
node_id: 5f0c7a52-3a43-4d43-8bd5-1b2f8f4b7b1e
enroll_token: secret-token
policy:
  allow_exec: false
`

func write(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadDefaultsAndURLs(t *testing.T) {
	c, err := Load(write(t, valid))
	if err != nil {
		t.Fatal(err)
	}
	if c.IntervalS != 10 || c.StateDir != "/var/lib/janus-agent" || c.ProcRoot != "/proc" {
		t.Fatalf("defaults: %+v", c)
	}
	if c.Host() != "janus.example.com:8443" {
		t.Fatalf("host = %q", c.Host())
	}
	if c.WebSocketURL() != "https://janus.example.com:8443/api/v1/agent/ws" {
		t.Fatalf("ws url = %q", c.WebSocketURL())
	}
}

func TestLoadRejects(t *testing.T) {
	for name, content := range map[string]string{
		"http server":   strings.Replace(valid, "https://", "http://", 1),
		"bad node id":   strings.Replace(valid, "5f0c7a52-", "x", 1),
		"interval":      valid + "interval_s: 60\n",
		"proxy":         valid + "proxy: socks5://p:1080\n",
		"unknown field": valid + "allow_everything: true\n",
	} {
		if _, err := Load(write(t, content)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestDropEnrollTokenKeepsTheRest(t *testing.T) {
	path := write(t, valid)
	c, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.DropEnrollToken(); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	if strings.Contains(string(raw), "secret-token") || !strings.Contains(string(raw), "# written by") {
		t.Fatalf("rewritten config:\n%s", raw)
	}
	st, _ := os.Stat(path)
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("mode = %v", st.Mode())
	}
	if again, err := Load(path); err != nil || again.EnrollToken != "" {
		t.Fatalf("reload: %v %+v", err, again)
	}
}
