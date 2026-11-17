# Releasing and signing the agent (FR-84)

The agent replaces itself only with a binary signed by the **release key** (ed25519). The server knows
only the **public** key (`JANUS_AGENT_RELEASE_PUBLIC_KEY`, written into each agent's config at bootstrap)
and distributes the signed files; it cannot forge a signature. For this to mean anything the **private
key must not live on the Janus-Ops server**: keep it on the workstation or machine you release from.

## One time: create the key

    docker run --rm -v "$PWD:/out" lordraw77/janus-ops:0.4.0 \
      agent-keygen --private-key /out/release.pem

It prints the public key. Put it in `deploy/.env` as `JANUS_AGENT_RELEASE_PUBLIC_KEY`, then reinstall each
agent once ("Reinstall / upgrade" on the node page) so that it carries the key. Keep `release.pem` in a
password manager or on an encrypted disk, and make a backup: **if the key is lost, every agent must be
reinstalled with a new one**; if it leaks, the same holds and the leaked key can run code as root on every
node until then.

## Every release: build, sign, publish

    make agent/build VERSION=0.4.1                 # agent/dist/janus-agent-linux-{amd64,arm64,arm}
    docker run --rm -v "$PWD/agent/dist:/dist" -v "$HOME/release.pem:/key:ro" \
      lordraw77/janus-ops:0.4.0 \
      agent-sign --private-key /key --dir /dist --agent-version 0.4.1

The image's entrypoint is `janus-ops`, so only the subcommand follows the image name. This writes a `janus-agent-linux-<arch>.sig` next to each binary (version, architecture, SHA-256 and the
signature over them). Copy the `dist` directory to the server and mount it with
`deploy/compose.release.yaml` (`JANUS_AGENT_RELEASE_DIR=/path/to/dist`). The key never has to be on the
server: only the binaries and the `.sig` files are.

## Roll out

On the node page use "Update (signed)", or "Update agents" in the inventory (or
`POST /api/v1/agents/update-all`) for every connected agent. Each agent checks the signature before it
receives the bytes, refuses anything that is not newer than what it runs, checks the SHA-256, replaces
itself and restarts; if the new binary cannot connect (or does not even start) the previous one is put
back automatically (agent rollback after 3 minutes or three failed starts, and a guard in the systemd unit
after four starts).

## Notes

- The signature covers version, architecture and hash: a binary cannot be passed off as another version or
  architecture, and an old signed binary cannot be used to downgrade an agent.
- Agents installed before the key was configured (or with an older unit file) are updated by reinstalling
  them from the node page, not by "Update (signed)".
- Signing in CI is possible with the same command if the key is stored as a CI secret; the key is then as
  exposed as the CI is.
