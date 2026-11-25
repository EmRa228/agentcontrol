#!/usr/bin/env bash
set -euo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo/scripts/opencode-versions.sh"
version="$opencode_version"
bun="$(command -v "${BUN_BIN:-bun}")"
if [[ "$("$bun" --version)" != "$bun_version" ]]; then
    echo "OpenCode $version requires Bun $bun_version for this build." >&2
    exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
git clone --depth 1 --branch "v$version" https://github.com/anomalyco/opencode.git "$work/source"
cd "$work/source"
test "$(git rev-parse HEAD)" = "$opencode_revision"
git apply "$repo/scripts/opencode-web-$version.patch"
"$bun" install --frozen-lockfile --ignore-scripts
cd packages/app
"$bun" run --bun build --base=/oc/ --sourcemap=false
test -s dist/index.html
printf '%s\n' "$version" > dist/OPENCODE_VERSION

# Prepare a complete release before switching the path served by Flask.
mkdir -p "$repo/.opencode-web-builds"
release="$(mktemp -d "$repo/.opencode-web-builds/$version.XXXXXXXX")"
cp -a dist/. "$release/"
link="$(mktemp -d "$repo/.opencode-web-link.XXXXXXXX")"
ln -s ".opencode-web-builds/${release##*/}" "$link/opencode-web"
mv -Tf -- "$link/opencode-web" "$repo/opencode-web"
rmdir -- "$link"
printf 'Installed OpenCode web %s at %s/opencode-web\n' "$version" "$repo"
