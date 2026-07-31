#!/usr/bin/env bash
set -euo pipefail

readonly BASELINE_COMMIT="a9f5f5da77ba231f23128fe76e21c6f93890b7ef"

if [[ "${1:-}" != "--confirm-rollback" ]]; then
  printf 'Usage: %s --confirm-rollback\n' "$0" >&2
  exit 2
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
git cat-file -e "${BASELINE_COMMIT}^{commit}"
if [[ -n "$(git status --porcelain)" ]]; then
  printf 'Working tree must be clean before rollback.\n' >&2
  exit 4
fi

before_head="$(git rev-parse HEAD)"
git reset --hard "$BASELINE_COMMIT"
after_head="$(git rev-parse HEAD)"
if [[ "$after_head" != "$BASELINE_COMMIT" ]]; then
  printf 'Rollback verification mismatch: expected %s, found %s\n' \
    "$BASELINE_COMMIT" "$after_head" >&2
  exit 5
fi
if [[ -n "$(git status --porcelain)" ]]; then
  printf 'Rollback left tracked working-tree changes.\n' >&2
  exit 6
fi
printf 'before_head=%s\n' "$before_head"
printf 'rollback_head=%s\n' "$after_head"
printf 'working_tree_clean=true\n'
