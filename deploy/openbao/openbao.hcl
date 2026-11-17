# OpenBao for Janus-Ops (FR-86): single node, integrated storage, TLS on the internal network,
# and automatic unseal with a static key so that a restart needs nobody (compose.openbao.yaml).
# The unseal key sits next to the data on this host: it protects a copy of the data volume, not a
# host that is already compromised. See docs/OPENBAO.md.
ui            = false
disable_mlock = true # integrated storage; the container has no IPC_LOCK
log_level     = "info"

storage "raft" {
  path    = "/openbao/file"
  node_id = "janus-1"
}

listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_cert_file   = "/openbao/tls/server.crt"
  tls_key_file    = "/openbao/tls/server.key"
  tls_min_version = "tls12"
}

api_addr     = "https://openbao:8200"
cluster_addr = "https://openbao:8201"

seal "static" {
  current_key_id = "janus-1"
  current_key    = "file:///openbao/secrets/seal.key"
}
