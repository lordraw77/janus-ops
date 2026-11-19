# Releasing and signing the agent (FR-84)

The agent replaces itself only with a binary signed by the **release key** (ed25519). The server knows
only the **public** key (`JANUS_AGENT_RELEASE_PUBLIC_KEY`, written into each agent's config at bootstrap)
and distributes the signed files; it cannot forge a signature. For this to mean anything the **private
key must not live on the Janus-Ops server**: keep it on the workstation or machine you release from.

## Where the key lives in this repository

`secrets/release/release.pem` (folder mode 700, file 600). The folder is ignored by git (`**/secrets/` in
`.gitignore`) and is outside the Docker build context (`.dockerignore` admits only `agent/`, `backend/`,
`frontend/` and `res/`, and excludes `*.pem`), so it cannot end up in a commit or in an image. `make
agent/sign` builds the agent and signs `agent/dist` with it. If this checkout is also the machine that runs
the stack, the key sits on the same host as the server: acceptable while you release from here, but
anyone who can read that folder can sign agents, so keep a copy elsewhere and move the key to the release
machine (or CI) when releases happen from somewhere else.

## One time: create the key

    docker run --rm -v "$PWD:/out" lordraw/janus-ops:0.8.2 \
      agent-keygen --private-key /out/release.pem

It prints the public key. Put it in `deploy/.env` as `JANUS_AGENT_RELEASE_PUBLIC_KEY`, then reinstall each
agent once ("Reinstall / upgrade" on the node page) so that it carries the key. Keep `release.pem` in a
password manager or on an encrypted disk, and make a backup: **if the key is lost, every agent must be
reinstalled with a new one**; if it leaks, the same holds and the leaked key can run code as root on every
node until then.

## Every release

The agent ships **inside the image** of each Janus-Ops version, so the signatures must ship with it, but
the private key must never enter CI or the image. That works because the agent build is reproducible
(`-trimpath -buildvcs=false`, pinned Go image): the binaries built on the release machine and the ones
built by the image are bit-for-bit the same, so a signature made on the release machine is valid for the
image's binaries. The signatures are public data and live in git.

1. Bump `__version__` in `backend/src/janus_ops/__init__.py`, merge, and make sure `main` is what you release.
2. `make agent/sign` builds the agent and signs it with `secrets/release/release.pem`, then copies the
   `.sig` files to **`agent/release/`**. Commit and push that directory.
3. `make release` (`scripts/release.sh`) checks that `agent/release/` is signed for this exact version and
   verifies the signatures against the public key (`agent-verify`) **before** it creates the tag, then pushes
   the tag.
4. CI builds the image with `REQUIRE_SIGNED=true`: the `Dockerfile` compares each signature's SHA-256 and
   version with the binary it just built and refuses to build on any mismatch or missing signature. A build
   without that flag (a developer `docker build`) still works, but the image cannot update agents and says so.

`janus-ops agent-verify --dir <dir> --public-key <key> [--agent-version X]` checks a directory of binaries
and `.sig` files by hand, for example inside a published image:

    docker run --rm lordraw/janus-ops:0.4.1 agent-verify --dir /app/agent --public-key "$JANUS_AGENT_RELEASE_PUBLIC_KEY"

A server that runs the new image already has the signed agents in `/app/agent`: nothing to mount. (The
`compose.release.yaml` override is only for signing by hand or testing a build that is not an image.)

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
