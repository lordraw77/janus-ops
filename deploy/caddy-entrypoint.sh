#!/bin/sh
# Starts Caddy and publishes the *public* root certificate of its internal CA (never the key)
# to /pki-public, where Janus-Ops reads it for the agents' ca_file (JANUS_AGENT_CA_FILE).
# With a public ACME certificate (JANUS_TLS=<email>) there is no internal CA: nothing is
# published and agents use the system CAs.
set -eu
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null' TERM INT

src=/data/caddy/pki/authorities/local/root.crt
(
  while kill -0 "$pid" 2>/dev/null; do
    if [ -f "$src" ] && ! cmp -s "$src" /pki-public/root.crt; then
      install -m 0644 "$src" /pki-public/root.crt.new && mv -f /pki-public/root.crt.new /pki-public/root.crt
    fi
    sleep 10
  done
) &

# the first wait returns when a signal arrives; the second waits for Caddy to finish
wait "$pid" || true
wait "$pid" 2>/dev/null || true
