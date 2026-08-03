#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--confirm-rollback" ]]; then
  echo "usage: $0 --confirm-rollback" >&2
  exit 2
fi

release_tag="v0.10.2"
previous_tag="v0.10.1"
release_commit="$(git rev-parse "$release_tag^{commit}")"
current_commit="$(git rev-parse HEAD)"
if [[ "$current_commit" != "$release_commit" ]]; then
  echo "rollback requires HEAD to be exactly $release_tag" >&2
  exit 2
fi

git rev-parse --verify "$previous_tag^{commit}" >/dev/null
rollback_dir="${TOPOFORGE_ROLLBACK_DIR:-../TopoForge-0.10.1}"
if [[ -e "$rollback_dir" ]]; then
  echo "rollback destination already exists: $rollback_dir" >&2
  exit 2
fi

git worktree add --detach "$rollback_dir" "$previous_tag"
test "$(git -C "$rollback_dir" rev-parse HEAD)" = "$(git rev-parse "$previous_tag^{commit}")"

cat <<EOF
TopoForge 0.10.1 source rollback worktree created at $rollback_dir.
The 0.10.2 checkout and retained DEMs, caches, outputs, backups, and Web workspaces were not changed.
EOF
