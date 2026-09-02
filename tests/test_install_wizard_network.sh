#!/usr/bin/env bash
# Shell tests for install-wizard network defaults.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=/dev/null
source <(sed -n '/^normalize_network_mode()/,/^}/p' "${ROOT}/install-wizard.sh")

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ "$(normalize_network_mode "")" == "1" ]] || fail 'empty -> direct'
[[ "$(normalize_network_mode "2")" == "2" ]] || fail '2 -> proxy'
[[ "$(normalize_network_mode "direct")" == "1" ]] || fail 'direct -> 1'
[[ "$(normalize_network_mode "bogus")" == "1" ]] || fail 'unknown -> direct default'

echo "install-wizard network defaults: OK"
