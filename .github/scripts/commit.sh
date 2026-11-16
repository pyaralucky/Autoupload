#!/usr/bin/env bash
# Commit and push whatever the tool changed. Usage: commit.sh "message"
set -euo pipefail
for path in queue posted failed reports QUEUE.md; do
  if [ -e "$path" ] || git ls-files --error-unmatch "$path" >/dev/null 2>&1; then
    git add -A -- "$path"
  fi
done
if git diff --cached --quiet; then
  echo "Nothing to commit."
  exit 0
fi
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git commit -m "$1"
git pull --rebase
git push
