#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--confirm-rollback" ]]; then
  echo "usage: $0 --confirm-rollback [--remove-generated] [--patch PATH]" >&2
  exit 2
fi
shift
remove_generated=false
patch="artifacts/patches/fidelity-aoi.patch"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --remove-generated)
      remove_generated=true
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
    outputs/gongga-copernicus-glo30-fidelity-v2 \
    outputs/gongga-copernicus-glo30-fidelity-v2-repeat \
    artifacts/slicer/gongga-fidelity-v2-prusa.gcode \
    artifacts/logs/gongga-fidelity-v2-build.log \
    artifacts/logs/gongga-fidelity-v2-repeat-build.log \
    artifacts/logs/gongga-fidelity-v2-prusa-slice.log \
    artifacts/logs/fidelity-aoi-quality-gates.log \
    artifacts/logs/fidelity-aoi-quality-gates-final.log \
    artifacts/verification/gongga-fidelity-v2-reread.txt \
    artifacts/verification/gongga-fidelity-v2-determinism.txt \
    artifacts/verification/gongga-fidelity-v2-verification.json \
    artifacts/verification/gongga-fidelity-v2-post-slice-bundle.json
fi

echo "fidelity/AOI source patch reversed"
