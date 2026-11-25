#!/usr/bin/env bash
set -euo pipefail
umask 022

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo/scripts/opencode-versions.sh"
state_dir="${STATE_DIR:-/var/lib/agentcontrol}"
bin_dir="$state_dir/opencode-bin"
mkdir -p "$bin_dir"
exec 9>"$state_dir/opencode-install.lock"
flock 9

managed="$bin_dir/opencode"
opencode="${OPENCODE_BIN:-}"
if [[ ! -x "$opencode" ]]; then
    opencode="$(command -v opencode || true)"
    for candidate in "$opencode" "$managed" "$HOME/.opencode/bin/opencode" /usr/local/bin/opencode; do
        if [[ -x "$candidate" ]]; then
            opencode="$candidate"
            break
        fi
    done
fi

version_of() {
    [[ -x "$1" ]] || return 1
    "$1" --version 2>/dev/null | tail -n 1 | sed 's/^opencode v//'
}

current_version="$(version_of "$opencode" || true)"
managed_version="$(version_of "$managed" || true)"
web_version=""
if [[ -s "$repo/opencode-web/index.html" && -f "$repo/opencode-web/OPENCODE_VERSION" ]]; then
    web_version="$(cat "$repo/opencode-web/OPENCODE_VERSION")"
fi

if [[ "$current_version" == "$opencode_version" && "$web_version" == "$opencode_version" ]]; then
    echo "OpenCode $opencode_version is already installed."
    exit 0
fi

case "$(uname -s):$(uname -m)" in
    Linux:x86_64)
        oc_asset=opencode-linux-x64-baseline.tar.gz
        oc_hash=9ddd18e3faf64eaeccf6a0fcbc96a5c7a14520ae5fd5b61e5be51d894f478b47
        bun_asset=bun-linux-x64-baseline
        bun_hash=e0446eb55995772eb9894bebae9a6be368d985a8b9271ee124486f9946eedd76
        ;;
    Linux:aarch64|Linux:arm64)
        oc_asset=opencode-linux-arm64.tar.gz
        oc_hash=cb28cf94541aeb62a4b2ea5997a99b5dd6bae595a772f36fbe279299f2032a4f
        bun_asset=bun-linux-aarch64
        bun_hash=baa6bb36f51d6e58561e02bbea825038a9df454f673d6d776e8bbf86f8c7850e
        ;;
    *) echo "OpenCode installation supports Linux x86_64 and ARM64." >&2; exit 1 ;;
esac

work="$(mktemp -d "$bin_dir/.install.XXXXXXXX")"
trap 'rm -rf -- "$work"' EXIT

download() {
    local url="$1" archive="$2" checksum="$3"
    local mirror="https://gh-proxy.com/${url}"
    local attempt
    for attempt in "$url" "$mirror"; do
        if curl --fail --silent --show-error --location --connect-timeout 15 --max-time 600 --retry 2 --output "$archive" "$attempt"; then
            if printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check; then
                return 0
            fi
            rm -f -- "$archive"
        fi
    done
    echo "Could not download $url" >&2
    return 1
}

# Keep the panel's private copy on the pinned release so upgrades never depend on
# whatever version `opencode` happens to be installed globally on the host.
if [[ "$current_version" != "$opencode_version" && "$managed_version" != "$opencode_version" ]]; then
    echo "Downloading OpenCode $opencode_version…"
    download "https://github.com/anomalyco/opencode/releases/download/v$opencode_version/$oc_asset" "$work/opencode.tar.gz" "$oc_hash"
    tar -xOzf "$work/opencode.tar.gz" opencode > "$work/opencode"
    chmod 755 "$work/opencode"
    test "$(version_of "$work/opencode")" = "$opencode_version"
    mv -f -- "$work/opencode" "$managed"
fi

if [[ "$web_version" != "$opencode_version" ]]; then
    bun="$(command -v "${BUN_BIN:-bun}" || true)"
    if [[ -z "$bun" || "$("$bun" --version)" != "$bun_version" ]]; then
        echo "Downloading Bun $bun_version…"
        download "https://github.com/oven-sh/bun/releases/download/bun-v$bun_version/$bun_asset.zip" "$work/bun.zip" "$bun_hash"
        python3 -m zipfile -e "$work/bun.zip" "$work"
        bun="$work/$bun_asset/bun"
        chmod 755 "$bun"
    fi
    echo "Building the OpenCode $opencode_version web frontend…"
    BUN_BIN="$bun" bash "$repo/scripts/build-opencode-web.sh"
fi
echo "OpenCode $opencode_version is ready."
