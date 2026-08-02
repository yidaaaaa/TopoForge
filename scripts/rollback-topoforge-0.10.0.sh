#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--confirm-rollback" ]]; then
  echo "usage: $0 --confirm-rollback" >&2
  exit 2
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "rollback requires a clean Git worktree" >&2
  exit 2
fi

git rev-parse --verify 'v0.9.0^{commit}' >/dev/null
release_commit="$(git rev-parse 'v0.10.0^{commit}')"
current_commit="$(git rev-parse HEAD)"
if [[ "$current_commit" != "$release_commit" ]]; then
  echo "rollback requires HEAD to be exactly v0.10.0" >&2
  exit 2
fi

git revert --no-edit v0.10.0

if ! git diff --quiet v0.9.0 HEAD --; then
  echo "rollback tree does not match v0.9.0" >&2
  exit 1
fi

cat <<'EOF'
TopoForge 0.10.0 source rollback completed.
The worktree now matches v0.9.0; retained DEMs, caches, outputs, backups, and Web workspaces were not deleted.
EOF
