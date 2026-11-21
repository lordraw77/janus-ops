//go:build windows

package config

const (
	// DefaultPath is the configuration file; the installer (`janus-agent install`) writes it.
	DefaultPath         = `C:\ProgramData\janus-agent\config.yaml`
	defaultStateDir     = `C:\ProgramData\janus-agent`
	defaultDockerSocket = `npipe:////./pipe/docker_engine`
)
