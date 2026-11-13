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

git tag -a "$TAG" -m "Janus-Ops $VERSION"
git push origin "$TAG"
echo "pushed $TAG: GitHub Actions will publish the image"
