#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:---check}"

for slug in great-trango-tower mount-thor; do
  bundle="$root/outputs/${slug}-example"
  backup="$root/artifacts/verification/${slug}-pre-bambu-project"
  case "$mode" in
    --check)
      test -f "$bundle/model.bambu-p2s.3mf"
      test -f "$bundle/bambu_project_validation.json"
      test -f "$backup/build_manifest.json"
      printf 'ready  %s\n' "$slug"
      ;;
    --apply)
      cp "$backup/build_manifest.json" "$bundle/build_manifest.json"
      cp "$backup/validation.json" "$bundle/validation.json"
      cp "$backup/provenance.json" "$bundle/provenance.json"
      rm -f -- \
        "$bundle/model.bambu-p2s.3mf" \
        "$bundle/bambu_project_validation.json" \
        "$bundle/bambu_project_build_result.json" \
        "$bundle/bambu_project_reopen_result.json" \
        "$bundle/bambu_project_build.stdout.log" \
        "$bundle/bambu_project_build.stderr.log" \
        "$bundle/bambu_project_reopen.stdout.log" \
        "$bundle/bambu_project_reopen.stderr.log" \
        "$bundle/OPEN_IN_BAMBU_STUDIO.txt"
      printf 'restored  %s\n' "$slug"
      ;;
    *)
      printf 'Usage: %s [--check|--apply]\n' "$0" >&2
      exit 2
      ;;
  esac
done
