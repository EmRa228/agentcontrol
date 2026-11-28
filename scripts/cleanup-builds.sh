#!/usr/bin/env bash
# Keep only the newest N OpenCode web builds (current symlink is always kept).
set -euo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
builds="$repo/.opencode-web-builds"
keep="${BUILD_KEEP:-3}"
[[ "$keep" =~ ^[0-9]+$ ]] || keep=3
[[ -d "$builds" ]] || { echo "No builds directory"; exit 0; }

current=""
[[ -L "$repo/opencode-web" ]] && current="$(readlink "$repo/opencode-web")"

mapfile -t dirs < <(find "$builds" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn | awk '{print $2}')
removed=0
for ((i=keep; i<${#dirs[@]}; i++)); do
  target="${dirs[$i]}"
  name="$(basename "$target")"
  if [[ -n "$current" && "$current" == *"$name"* ]]; then
    continue
  fi
  rm -rf -- "$target"
  removed=$((removed + 1))
done
echo "Kept $(( ${#dirs[@]} - removed )) build(s); removed $removed"
