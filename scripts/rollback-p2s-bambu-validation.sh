#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage: scripts/rollback-p2s-bambu-validation.sh --confirm-rollback [--patch PATH] [--source-only]' \
    '' \
    'Reverses the P2S/Bambu Studio source patch and, by default, restores the preserved pre-final 3MF/G-code evidence.'
}

confirm=false
source_only=false
patch_path='artifacts/patches/p2s-bambu-validation.patch'
while (($#)); do
  case "$1" in
    --confirm-rollback)
      confirm=true
      shift
      ;;
    --patch)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      patch_path="$2"
      shift 2
      ;;
    --source-only)
      source_only=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$confirm" != true ]]; then
  printf '%s\n' 'Rollback confirmation missing; pass --confirm-rollback.' >&2
  exit 2
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
if [[ "$patch_path" != /* ]]; then
  patch_path="$repo_root/$patch_path"
fi
[[ -s "$patch_path" ]] || { printf 'Patch not found: %s\n' "$patch_path" >&2; exit 2; }

out='outputs/gongga-copernicus-glo30-bambu-p2s'
backup="$out/.rollback-pre-final-20260731"
primary_gcode='artifacts/slicer/gongga-copernicus-glo30-bambu-p2s.gcode'
reopen_gcode='artifacts/slicer/gongga-copernicus-glo30-bambu-p2s-reopen.gcode'
expected_final_3mf='27aa850ebe666c14cc061dfcba97a42d4729a0bd9a37fd0a4d1888961386a55f'
expected_baseline_3mf='898610a7b3094ed51d5ff9bb8e1f5701eee93ecd666c1e1d9e0cf61556f8d27d'
expected_baseline_gcode='27b43eb59965ae93c0f0867ca3c63b38d19fc13f97a5aeba3486366b8a19654d'

if [[ "$source_only" != true ]]; then
  [[ -d "$backup" ]] || { printf 'Artifact backup not found: %s\n' "$backup" >&2; exit 3; }
  current_3mf="$(sha256sum "$out/model.bambu-p2s.3mf" | awk '{print $1}')"
  backup_3mf="$(sha256sum "$backup/model.bambu-p2s.3mf" | awk '{print $1}')"
  backup_gcode="$(sha256sum "$backup/gongga-copernicus-glo30-bambu-p2s.gcode" | awk '{print $1}')"
  [[ "$current_3mf" == "$expected_final_3mf" ]] || { printf '%s\n' 'Current P2S 3MF is not the verified final artifact.' >&2; exit 4; }
  [[ "$backup_3mf" == "$expected_baseline_3mf" ]] || { printf '%s\n' 'Preserved 3MF baseline hash mismatch.' >&2; exit 5; }
  [[ "$backup_gcode" == "$expected_baseline_gcode" ]] || { printf '%s\n' 'Preserved G-code baseline hash mismatch.' >&2; exit 6; }
fi

printf 'Baseline HEAD: %s\n' "$(git rev-parse HEAD)"
printf 'Patch: %s\n' "$patch_path"
git apply --reverse --check "$patch_path"
git apply --reverse "$patch_path"

if [[ "$source_only" != true ]]; then
  for source in "$backup"/*; do
    [[ -f "$source" ]] || continue
    name="$(basename "$source")"
    if [[ "$name" == 'gongga-copernicus-glo30-bambu-p2s.gcode' ]]; then
      cp -a "$source" "$primary_gcode"
    else
      cp -a "$source" "$out/$name"
    fi
  done
  rm -f \
    "$reopen_gcode" \
    "$out/bambu_studio_build_command.txt" \
    "$out/bambu_studio_reopen_command.txt" \
    "$out/bambu_p2s_prepare_profiles.sh"
  restored_3mf="$(sha256sum "$out/model.bambu-p2s.3mf" | awk '{print $1}')"
  restored_gcode="$(sha256sum "$primary_gcode" | awk '{print $1}')"
  [[ "$restored_3mf" == "$expected_baseline_3mf" ]]
  [[ "$restored_gcode" == "$expected_baseline_gcode" ]]
  printf 'restored_3mf_sha256=%s\n' "$restored_3mf"
  printf 'restored_gcode_sha256=%s\n' "$restored_gcode"
fi

git diff --check
printf '%s\n' 'Rollback applied successfully.'
git status --short --branch
