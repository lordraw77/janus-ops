#!/usr/bin/env bash
# Tag the current commit as v<VERSION> and push the tag.
# The tag triggers .github/workflows/ci.yml, which builds the multi-arch image and pushes
# it to Docker Hub (secrets DOCKERHUB_USERNAME / DOCKERHUB_TOKEN).
# Usage: scripts/release.sh VERSION      (normally via `make release`)
set -euo pipefail

VERSION="${1:?version required}"
TAG="v${VERSION}"

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "invalid version: $VERSION" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "working tree not clean" >&2; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || { echo "release from main only" >&2; exit 1; }
git fetch -q origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || {
  echo "local main differs from origin/main: push or pull first" >&2; exit 1; }
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  echo "tag $TAG already exists" >&2; exit 1
fi

# The agent signatures in agent/release/ must be the ones for THIS version's binaries (the image build
# is reproducible, so a signature made here matches what CI builds). Check before the tag exists: CI
# would refuse the image otherwise. The public key comes from the environment or deploy/.env.
PUBKEY="${JANUS_AGENT_RELEASE_PUBLIC_KEY:-$(sed -n 's/^JANUS_AGENT_RELEASE_PUBLIC_KEY=//p' deploy/.env 2>/dev/null | head -1)}"
[ -n "$PUBKEY" ] || { echo "JANUS_AGENT_RELEASE_PUBLIC_KEY is not set (env or deploy/.env): cannot check the agent signatures" >&2; exit 1; }
VERIFY_DIR="$(mktemp -d)"
trap 'rm -rf "$VERIFY_DIR"' EXIT
make agent/build VERSION="$VERSION" >/dev/null
cp agent/dist/janus-agent-linux-* agent/dist/janus-agent-windows-amd64.exe "$VERIFY_DIR/"
cp agent/release/*.sig "$VERIFY_DIR/" 2>/dev/null || { echo "no agent/release/*.sig: run 'make agent/sign' and commit" >&2; exit 1; }
(cd backend && uv run janus-ops agent-verify --dir "$VERIFY_DIR" --public-key "$PUBKEY" --agent-version "$VERSION") \
  || { echo "agent signatures do not match: run 'make agent/sign' (with secrets/release/release.pem) and commit agent/release/" >&2; exit 1; }

git tag -a "$TAG" -m "Janus-Ops $VERSION"
git push origin "$TAG"
echo "pushed $TAG: GitHub Actions will publish the image"
