#!/usr/bin/env bash
set -euo pipefail

readonly BASELINE_COMMIT="10112b0a827bd27db6054d3ecf01a47d62b4aed5"

if [[ "${1:-}" != "--confirm-rollback" ]]; then
  printf 'Usage: %s --confirm-rollback\n' "$0" >&2
  exit 64
fi

repository_root="$(git rev-parse --show-toplevel)"
cd "$repository_root"

git cat-file -e "${BASELINE_COMMIT}^{commit}"
git reset --hard "$BASELINE_COMMIT"
git clean -fdx

actual_head="$(git rev-parse HEAD)"
if [[ "$actual_head" != "$BASELINE_COMMIT" ]]; then
  printf 'Rollback verification mismatch: expected %s, found %s\n' \
    "$BASELINE_COMMIT" "$actual_head" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  printf 'Rollback verification found residual working-tree changes.\n' >&2
  exit 1
fi

printf 'rollback_head=%s\n' "$actual_head"
printf 'rollback_status=clean\n'
