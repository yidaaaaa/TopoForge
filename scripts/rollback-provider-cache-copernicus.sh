#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--confirm-rollback" ]]; then
  echo "usage: $0 --confirm-rollback [--remove-generated] [--remove-cache] [--patch PATH]" >&2
  exit 2
fi
shift
remove_generated=false
remove_cache=false
patch="artifacts/patches/provider-cache-copernicus.patch"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --remove-generated)
      remove_generated=true
      shift
      ;;
    --remove-cache)
      remove_cache=true
      shift
      ;;
    --patch)
      patch="$2"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
patch="$(realpath "$patch")"
git apply --reverse --check "$patch"
git apply --reverse "$patch"

if [[ "$remove_generated" == true ]]; then
  rm -rf \
    outputs/amazon-copernicus-aws-v2 \
    downloads/amazon-copernicus-aws-v1 \
    downloads/amazon-copernicus-aws-v2 \
    artifacts/slicer/amazon-copernicus-aws-v2-prusa.gcode \
    artifacts/logs/amazon-copernicus-aws-v1-build.log \
    artifacts/logs/amazon-copernicus-aws-v2-build.log \
    artifacts/logs/amazon-copernicus-aws-v2-prusa-slice.log \
    artifacts/logs/provider-cache-copernicus-quality-gates.log \
    artifacts/verification/amazon-copernicus-aws-v2-verification.json
fi
if [[ "$remove_cache" == true ]]; then
  rm -rf cache/providers
fi

echo "provider/cache/Copernicus source patch reversed"
