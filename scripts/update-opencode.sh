#!/usr/bin/env bash
# Safe OpenCode upgrade pipeline.
#
#   scripts/update-opencode.sh --check                 # report latest version
#   scripts/update-opencode.sh --version 2.4.2         # upgrade (needs a patch)
#   scripts/update-opencode.sh --reuse-patch 2.4.1     # try 3-way with an older patch
#
# Verifies release digests, applies the frontend patch with `git apply --3way`,
# builds with the pinned Bun, smoke-tests the output, swaps `opencode-web`
# atomically and updates scripts/opencode-versions.sh. On failure it restores the
# previous pin/symlink/binary and exits non-zero.
set -euo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
state_dir="${STATE_DIR:-/var/lib/agentcontrol}"
bin_dir="$state_dir/opencode-bin"
versions_file="$repo/scripts/opencode-versions.sh"
current_version="$(sed -n 's/^opencode_version=//p' "$versions_file")"
target=""
reuse_patch=""
dry_run=0

usage() { sed -n '2,12p' "$0"; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) dry_run=2; shift ;;
    --dry-run) dry_run=1; shift ;;
    --version) target="${2:-}"; shift 2 ;;
    --version=*) target="${1#*=}"; shift ;;
    --reuse-patch) reuse_patch="${2:-}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 1 ;;
  esac
done

log() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

resolve_latest() {
  curl -fsS --max-time 20 "https://api.github.com/repos/anomalyco/opencode/releases?per_page=30" \
    | python3 -c 'import json,sys
rows=[]
for rel in json.load(sys.stdin):
    tag=(rel.get("tag_name") or "").lstrip("v")
    if rel.get("prerelease") or not tag: continue
    try: rows.append((tuple(int(p) for p in tag.split(".")), tag))
    except ValueError: pass
print(max(rows)[1] if rows else "")'
}

if [[ "$dry_run" == 2 ]]; then
  latest="$(resolve_latest)"
  [[ -n "$latest" ]] || die "could not resolve the latest release"
  echo "installed: $current_version"
  echo "latest:    $latest"
  [[ "$latest" == "$current_version" ]] && echo "up to date" || echo "update available"
  exit 0
fi

[[ -n "$target" ]] || target="$(resolve_latest)"
[[ -n "$target" ]] || die "could not resolve a target version"
target="${target#v}"
log "target OpenCode $target (installed $current_version)"

release_json="$state_dir/.opencode-release-$target.json"
curl -fsS --max-time 30 "https://api.github.com/repos/anomalyco/opencode/releases/tags/v$target" -o "$release_json"
mapfile -t meta < <(python3 - "$release_json" <<'PY'
import json,sys
data=json.load(open(sys.argv[1]))
assets={a["name"]: (a.get("digest") or "") for a in data.get("assets",[])}
print(assets.get("opencode-linux-x64-baseline.tar.gz","").removeprefix("sha256:"))
print(assets.get("opencode-linux-arm64.tar.gz","").removeprefix("sha256:"))
PY
)
x64_digest="${meta[0]:-}"
arm_digest="${meta[1]:-}"
[[ -n "$x64_digest" && -n "$arm_digest" ]] || die "release assets/digests missing for v$target"

patch_file="$repo/scripts/opencode-web-$target.patch"
if [[ -n "$reuse_patch" ]]; then
  patch_file="$repo/scripts/opencode-web-${reuse_patch#v}.patch"
fi
[[ -f "$patch_file" ]] || die "no frontend patch for v$target: create scripts/opencode-web-$target.patch (or pass --reuse-patch <ver>)"

work="$(mktemp -d)"
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT

download_verified() {
  local url="$1" out="$2" digest="$3" attempt
  for attempt in "$url" "https://gh-proxy.com/${url}"; do
    if curl --fail --silent --show-error --location --connect-timeout 15 --max-time 600 --retry 2 --output "$out" "$attempt"; then
      if printf '%s  %s\n' "$digest" "$out" | sha256sum --check --status; then
        return 0
      fi
      log "checksum mismatch from $attempt"
      rm -f -- "$out"
    fi
  done
  return 1
}

log "resolving source revision"
revision="$(git ls-remote "https://github.com/anomalyco/opencode.git" "refs/tags/v$target" 2>/dev/null | awk '{print $1}' | head -n1)"
[[ -n "$revision" ]] || die "could not resolve the tag commit for v$target"

log "downloading source v$target"
curl -fsSL --max-time 300 -o "$work/source.tar.gz" "https://github.com/anomalyco/opencode/archive/refs/tags/v$target.tar.gz" \
  || die "source download failed"
mkdir -p "$work/source"
tar -xzf "$work/source.tar.gz" -C "$work/source" --strip-components=1

log "applying $(basename "$patch_file") (3-way)"
cd "$work/source"
git init -q
git config user.email agentcontrol@localhost
git config user.name agentcontrol
git add -A >/dev/null 2>&1
git commit -q -m "opencode v$target" >/dev/null 2>&1
if ! git apply --3way "$patch_file" 2>"$work/patch.err"; then
  log "git apply --3way failed; trying patch -p1"
  if ! patch -p1 --forward --batch < "$patch_file" >>"$work/patch.log" 2>&1; then
    cat "$work/patch.err" >&2
    cat "$work/patch.log" >&2 || true
    die "patch does not apply to v$target — update scripts/opencode-web-$target.patch"
  fi
fi

source "$versions_file"
bun="$(command -v "${BUN_BIN:-bun}" || true)"
if [[ -z "$bun" || "$("$bun" --version)" != "$bun_version" ]]; then
  log "downloading Bun $bun_version"
  case "$(uname -m)" in
    x86_64|amd64) bun_asset=bun-linux-x64-baseline; bun_hash=e0446eb55995772eb9894bebae9a6be368d985a8b9271ee124486f9946eedd76 ;;
    aarch64|arm64) bun_asset=bun-linux-aarch64; bun_hash=baa6bb36f51d6e58561e02bbea825038a9df454f673d6d776e8bbf86f8c7850e ;;
    *) die "unsupported architecture $(uname -m)" ;;
  esac
  download_verified "https://github.com/oven-sh/bun/releases/download/bun-v$bun_version/$bun_asset.zip" "$work/bun.zip" "$bun_hash" || die "Bun download failed"
  python3 -m zipfile -e "$work/bun.zip" "$work"
  bun="$work/$bun_asset/bun"
  chmod 755 "$bun"
fi

log "building the web frontend"
(
  cd "$work/source"
  "$bun" install --frozen-lockfile --ignore-scripts
  cd packages/app
  "$bun" run --bun build --base=/oc/ --sourcemap=false
  test -s dist/index.html
  printf '%s\n' "$target" > dist/OPENCODE_VERSION
) >"$work/build.log" 2>&1 || {
  tail -n 80 "$work/build.log" >&2
  die "frontend build failed"
}

[[ -s "$work/source/packages/app/dist/index.html" ]] || die "build output missing index.html"
[[ -d "$work/source/packages/app/dist/assets" ]] || die "build output missing assets/"

if [[ "$dry_run" == 1 ]]; then
  log "dry run: build succeeded; web, pins and binary unchanged"
  exit 0
fi

web_prev="$(readlink "$repo/opencode-web" || true)"
binary_prev="$bin_dir/opencode"
[[ -f "$binary_prev" ]] && cp -a "$binary_prev" "$work/opencode.previous" || true
cp -a "$versions_file" "$work/versions.backup"
success=0

rollback() {
  [[ "$success" == "1" ]] && return 0
  cp -a "$work/versions.backup" "$versions_file" 2>/dev/null || true
  if [[ -n "$web_prev" ]]; then
    ln -sfn "$web_prev" "$repo/opencode-web"
  fi
  if [[ -f "$work/opencode.previous" ]]; then
    mv -f "$work/opencode.previous" "$binary_prev"
  fi
}
trap 'rollback; cleanup' EXIT

log "installing the web build atomically"
mkdir -p "$repo/.opencode-web-builds"
release="$(mktemp -d "$repo/.opencode-web-builds/$target.XXXXXXXX")"
cp -a "$work/source/packages/app/dist/." "$release/"
link="$(mktemp -d "$repo/.opencode-web-link.XXXXXXXX")"
ln -s ".opencode-web-builds/${release##*/}" "$link/opencode-web"
mv -Tf -- "$link/opencode-web" "$repo/opencode-web"
rmdir -- "$link"

[[ -s "$repo/opencode-web/index.html" ]] || die "web build missing index.html"
[[ -d "$repo/opencode-web/assets" ]] || die "web build missing assets/"
[[ "$(cat "$repo/opencode-web/OPENCODE_VERSION" 2>/dev/null)" == "$target" ]] || die "web build version marker mismatch"
log "web smoke test passed"

case "$(uname -m)" in
  x86_64|amd64) asset=opencode-linux-x64-baseline.tar.gz; digest="$x64_digest" ;;
  aarch64|arm64) asset=opencode-linux-arm64.tar.gz; digest="$arm_digest" ;;
  *) die "unsupported architecture $(uname -m)" ;;
esac
log "downloading binary $asset"
download_verified "https://github.com/anomalyco/opencode/releases/download/v$target/$asset" "$work/opencode.tar.gz" "$digest" \
  || die "binary download/checksum failed"
tar -xOzf "$work/opencode.tar.gz" opencode > "$work/opencode"
chmod 755 "$work/opencode"
[[ "$("$work/opencode" --version 2>/dev/null | tail -n1 | sed 's/^opencode v//')" == "$target" ]] \
  || die "downloaded binary reports an unexpected version"

mkdir -p "$bin_dir"
mv -f -- "$work/opencode" "$binary_prev"

log "updating pins"
python3 - "$versions_file" "$target" "$revision" "$x64_digest" "$arm_digest" <<'PY'
import pathlib, re, sys
path, version, revision, x64, arm = sys.argv[1:6]
text = pathlib.Path(path).read_text()
text = re.sub(r"^opencode_version=.*$", f"opencode_version={version}", text, flags=re.M)
text = re.sub(r"^opencode_revision=.*$", f"opencode_revision={revision}", text, flags=re.M)
out, section = [], ""
for line in text.splitlines():
    if line.startswith("Linux:x86_64"):
        section = "x64"
    elif line.startswith("Linux:aarch64"):
        section = "arm"
    elif section and line.strip().startswith("oc_hash="):
        line = f"        oc_hash={x64 if section == 'x64' else arm}"
    elif section and line.strip().startswith("}"):
        section = ""
    out.append(line)
pathlib.Path(path).write_text("\n".join(out) + "\n")
PY

if ! "$binary_prev" --version >/dev/null 2>&1; then
  die "binary smoke test failed"
fi

success=1
bash "$repo/scripts/cleanup-builds.sh" >/dev/null 2>&1 || true
log "OpenCode $target installed (web + binary). It is used on the next /api/opencode/open request."
