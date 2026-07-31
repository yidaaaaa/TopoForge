#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:---check}"
if [[ "${2:-}" == "--root" && -n "${3:-}" ]]; then
  repo_root="$(cd "$3" && pwd)"
fi

paths=(
  examples/real-terrain
  downloads/examples/great-trango-tower-glo30.tif
  downloads/examples/great-trango-tower-glo30.tif.source_acquisition.json
  downloads/examples/mount-thor-glo30.tif
  downloads/examples/mount-thor-glo30.tif.source_acquisition.json
  outputs/great-trango-tower-example
  outputs/mount-thor-example
  artifacts/slicer/great-trango-tower-p2s.gcode
  artifacts/slicer/mount-thor-p2s.gcode
  artifacts/verification/terrain-examples-verification.json
  artifacts/verification/terrain-examples-summary.json
  artifacts/verification/terrain-examples-rollback-test.txt
  artifacts/patches/terrain-examples.patch
  artifacts/previews/great-trango-tower-example.jpg
  artifacts/previews/mount-thor-example.jpg
  artifacts/previews/great-trango-tower-glo30-quicklook.jpg
  artifacts/previews/great-trango-tower-glo30-quicklook.png
  artifacts/previews/mount-thor-glo30-quicklook.jpg
  artifacts/previews/mount-thor-glo30-quicklook.png
  artifacts/logs/great-trango-tower-fetch.log
  artifacts/logs/great-trango-tower-build.log
  artifacts/logs/great-trango-tower-bambu-slice.log
  artifacts/logs/mount-thor-fetch.log
  artifacts/logs/mount-thor-build.log
  artifacts/logs/mount-thor-bambu-slice.log
)

case "$mode" in
  --check)
    printf 'Rollback root: %s\n' "$repo_root"
    for path in "${paths[@]}"; do
      [[ -e "$repo_root/$path" ]] && printf 'present  %s\n' "$path" || printf 'absent   %s\n' "$path"
    done
    ;;
  --apply)
    for path in "${paths[@]}"; do
      rm -rf -- "$repo_root/$path"
    done
    printf 'Removed terrain-example task artifacts from %s\n' "$repo_root"
    printf 'Shared content-addressed provider cache was intentionally preserved.\n'
    ;;
  *)
    printf 'Usage: %s [--check|--apply] [--root PATH]\n' "$0" >&2
    exit 2
    ;;
esac
