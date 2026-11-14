// janus-agent: outbound-only telemetry agent for Janus-Ops (proto/agent-protocol.md).
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/lordraw77/janus-ops/agent/internal/buffer"
	"github.com/lordraw77/janus-ops/agent/internal/collect"
	"github.com/lordraw77/janus-ops/agent/internal/config"
	"github.com/lordraw77/janus-ops/agent/internal/identity"
	"github.com/lordraw77/janus-ops/agent/internal/tunnel"
)

// version is set at build time: -ldflags "-X main.version=..."
var version = "dev"

const bufferMaxAge = 24 * time.Hour

func usage() {
	fmt.Fprintf(os.Stderr, `usage: janus-agent [run] [-config PATH]
       janus-agent collect [-config PATH]   print one sample and the facts as JSON
       janus-agent version
`)
}

func main() {
	cmd := "run"
	args := os.Args[1:]
	if len(args) > 0 && args[0] != "" && args[0][0] != '-' {
		cmd, args = args[0], args[1:]
	}
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	path := fs.String("config", config.DefaultPath, "configuration file")
	fs.Usage = usage
	_ = fs.Parse(args)

	switch cmd {
	case "version":
		fmt.Println(version)
	case "run":
		os.Exit(run(*path))
	case "collect":
		os.Exit(collectOnce(*path))
	default:
		usage()
		os.Exit(2)
	}
}

func run(path string) int {
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	cfg, err := config.Load(path)
	if err != nil {
		log.Error("configuration", "err", err)
		return 1
	}
	id, err := identity.LoadOrCreate(cfg.StateDir)
	if err != nil {
		log.Error("identity key", "err", err)
		return 1
	}
	buf, err := buffer.Open(filepath.Join(cfg.StateDir, "buffer.db"), bufferMaxAge)
	if err != nil {
		log.Error("buffer", "err", err)
		return 1
	}
	defer buf.Close()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	col := collect.New(cfg.ProcRoot, cfg.SysRoot)
	log.Info("starting", "version", version, "node_id", cfg.NodeID, "server", cfg.ServerURL,
		"interval_s", cfg.IntervalS, "policy", cfg.Policy)
	if err := tunnel.New(cfg, id, buf, col, version, log).Run(ctx); err != nil {
		log.Error("stopped", "err", err)
		return 1
	}
	log.Info("stopped")
	return 0
}

func collectOnce(path string) int {
	proc, sys := "/proc", "/sys"
	if cfg, err := config.Load(path); err == nil {
		proc, sys = cfg.ProcRoot, cfg.SysRoot
	}
	col := collect.New(proc, sys)
	start := time.Now()
	col.Sample(start)
	col.TopProcesses(5, 0)
	time.Sleep(time.Second)
	out := map[string]any{"sample": col.Sample(time.Now()), "facts": col.Facts(time.Since(start)), "info": col.Info()}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(out); err != nil {
		return 1
	}
	return 0
}
