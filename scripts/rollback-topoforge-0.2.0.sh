#!/usr/bin/env bash
set -euo pipefail

readonly RELEASE_COMMIT="d70e624"

if [[ "${1:-}" != "--confirm-rollback" ]]; then
  printf 'Usage: %s --confirm-rollback\n' "$0" >&2
  exit 2
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
git cat-file -e "${RELEASE_COMMIT}^{commit}"
if ! git merge-base --is-ancestor "$RELEASE_COMMIT" HEAD; then
  printf 'Release commit %s is not an ancestor of HEAD.\n' "$RELEASE_COMMIT" >&2
  exit 3
fi
if [[ -n "$(git status --porcelain)" ]]; then
  printf 'Working tree must be clean before rollback.\n' >&2
  exit 4
fi

git revert --no-edit "$RELEASE_COMMIT"
printf 'reverted_release_commit=%s\n' "$RELEASE_COMMIT"
printf 'rollback_commit=%s\n' "$(git rev-parse HEAD)"
printf 'working_tree_clean=%s\n' "$(test -z "$(git status --porcelain)" && echo true || echo false)"
