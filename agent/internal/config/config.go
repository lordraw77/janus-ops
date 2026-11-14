// Package config reads /etc/janus-agent/config.yaml, written by the server's SSH bootstrap.
package config

import (
	"bytes"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

const DefaultPath = "/etc/janus-agent/config.yaml"

type Config struct {
	ServerURL   string       `yaml:"server_url"`
	NodeID      string       `yaml:"node_id"`
	EnrollToken string       `yaml:"enroll_token"`
	CAFile      string       `yaml:"ca_file"`
	Proxy       string       `yaml:"proxy"`
	IntervalS   int          `yaml:"interval_s"`
	StateDir    string       `yaml:"state_dir"`
	Policy      proto.Policy `yaml:"policy"`
	// Root of procfs and sysfs: changed only by tests and containerised agents.
	ProcRoot string `yaml:"proc_root"`
	SysRoot  string `yaml:"sys_root"`

	path string
}

var uuidRe = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

// Load reads and validates the configuration; defaults fill the optional fields.
func Load(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	c := &Config{path: path}
	dec := yaml.NewDecoder(bytes.NewReader(raw))
	dec.KnownFields(true)
	if err := dec.Decode(c); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if err := c.validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return c, nil
}

func (c *Config) validate() error {
	u, err := url.Parse(c.ServerURL)
	if err != nil || u.Scheme != "https" || u.Host == "" {
		return errors.New("server_url must be an https:// URL")
	}
	if !uuidRe.MatchString(c.NodeID) {
		return errors.New("node_id must be a UUID")
	}
	if c.Proxy != "" {
		p, err := url.Parse(c.Proxy)
		if err != nil || (p.Scheme != "http" && p.Scheme != "https") || p.Host == "" {
			return errors.New("proxy must be an http:// or https:// URL")
		}
	}
	if c.IntervalS == 0 {
		c.IntervalS = 10
	}
	if c.IntervalS < 5 || c.IntervalS > 15 {
		return errors.New("interval_s must be between 5 and 15")
	}
	if c.StateDir == "" {
		c.StateDir = "/var/lib/janus-agent"
	}
	if c.ProcRoot == "" {
		c.ProcRoot = "/proc"
	}
	if c.SysRoot == "" {
		c.SysRoot = "/sys"
	}
	return nil
}

// Host is host[:port] of the server, part of the signed challenge.
func (c *Config) Host() string {
	u, _ := url.Parse(c.ServerURL)
	return u.Host
}

// WebSocketURL is the gateway endpoint under server_url.
func (c *Config) WebSocketURL() string {
	return strings.TrimRight(c.ServerURL, "/") + "/api/v1/agent/ws"
}

// DropEnrollToken removes the one-time token from the file once the server confirmed the
// enrollment. Lines are filtered rather than re-encoded, so comments survive.
func (c *Config) DropEnrollToken() error {
	if c.EnrollToken == "" {
		return nil
	}
	raw, err := os.ReadFile(c.path)
	if err != nil {
		return err
	}
	var kept []string
	for _, line := range strings.Split(string(raw), "\n") {
		if !strings.HasPrefix(strings.TrimSpace(line), "enroll_token:") {
			kept = append(kept, line)
		}
	}
	tmp, err := os.CreateTemp(filepath.Dir(c.path), ".config-*.yaml")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name()) // no-op after the rename
	if _, err := tmp.WriteString(strings.Join(kept, "\n")); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp.Name(), c.path); err != nil {
		return err
	}
	c.EnrollToken = ""
	return nil
}
