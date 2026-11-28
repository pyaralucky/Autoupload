#!/usr/bin/env bash
# Publish the next queued post by committing a change to .github/post-now.
# Usage: post-now.sh slot    (counts as the scheduled slot that is due)
#        post-now.sh extra   (an additional post)
set -euo pipefail
mode="${1:-extra}"
cd "$(git rev-parse --show-toplevel)"
branch="$(git symbolic-ref --short HEAD)"
git pull -q --no-rebase origin "$branch"
printf 'mode: %s\nChange this file to publish the next queued post immediately.\nLast requested: %s\n' \
  "$mode" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > .github/post-now
git add .github/post-now
git commit -q -m "Publish the next queued post ($mode)"
git push -q origin "$branch"
git rev-parse HEAD
