//go:build !windows

package config

const (
	// DefaultPath is the configuration file, written by the server's SSH bootstrap.
	DefaultPath         = "/etc/janus-agent/config.yaml"
	defaultStateDir     = "/var/lib/janus-agent"
	defaultDockerSocket = "/var/run/docker.sock"
)
