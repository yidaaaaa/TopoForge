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

git rev-parse --verify 'v0.7.0^{commit}' >/dev/null
git rev-parse --verify 'v0.8.0^{commit}' >/dev/null

git revert --no-edit v0.8.0

if ! git diff --quiet v0.7.0 HEAD --; then
  echo "rollback tree does not match v0.7.0" >&2
  exit 1
fi

cat <<'EOF'
TopoForge 0.8.0 source rollback completed.
The worktree now matches v0.7.0; retained DEMs, caches, outputs, and Web workspaces were not deleted.
EOF
