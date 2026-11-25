#!/usr/bin/env bash
set -euo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
version=1.19.6
revision=f4e363c8295fa90a03dddc451882fffe8fbce357
bun="$(command -v "${BUN_BIN:-bun}")"
if [[ "$("$bun" --version)" != 1.4.4 ]]; then
    echo "OpenCode $version requires Bun 1.4.4 for this build." >&2
    exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
git clone --depth 1 --branch "v$version" https://github.com/anomalyco/opencode.git "$work/source"
cd "$work/source"
test "$(git rev-parse HEAD)" = "$revision"
git apply "$repo/scripts/opencode-web-$version.patch"
"$bun" install --frozen-lockfile --ignore-scripts
cd packages/app
"$bun" run --bun build --base=/oc/ --sourcemap=false
test -s dist/index.html

# Prepare a complete release before switching the path served by Flask.
mkdir -p "$repo/.opencode-web-builds"
release="$(mktemp -d "$repo/.opencode-web-builds/$version.XXXXXXXX")"
cp -a dist/. "$release/"
link="$(mktemp -d "$repo/.opencode-web-link.XXXXXXXX")"
ln -s ".opencode-web-builds/${release##*/}" "$link/opencode-web"
mv -Tf -- "$link/opencode-web" "$repo/opencode-web"
rmdir -- "$link"
printf 'Installed OpenCode web %s at %s/opencode-web\n' "$version" "$repo"
