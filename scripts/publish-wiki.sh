#!/usr/bin/env bash
# Regenerates the GitHub wiki from the in-app Guide and publishes it to <repo>.wiki.git.
#   scripts/publish-wiki.sh            generate, commit and push the wiki
#   scripts/publish-wiki.sh --dry-run  generate and show what would change, push nothing
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
remote="${WIKI_REMOTE:-$(git -C "$here" remote get-url origin | sed 's/\.git$//').wiki.git}"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT

node "$here/scripts/gen-wiki.mjs" "$here/docs/wiki" 2>/dev/null
git clone -q "$remote" "$work"
find "$work" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -r "$here/docs/wiki/." "$work/"
cd "$work"
git add -A
if git diff --cached --quiet; then echo "wiki already up to date"; exit 0; fi
git --no-pager diff --cached --stat | tail -5
[ "${1:-}" = "--dry-run" ] && { echo "dry run: nothing pushed"; exit 0; }
git commit -q -m "Wiki: update from the in-app Guide"
git push -q origin HEAD
echo "wiki published: ${remote%.wiki.git}/wiki"
