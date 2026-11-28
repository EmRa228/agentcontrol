#!/usr/bin/env bash
# Make the bundled Caddy layout the default for this install.
#
# Old installs ran Flask directly on `port`. This script adds the loopback
# internal ports to config.yaml so the panel can sit behind Caddy (TLS on the
# public port, plain HTTP redirected to HTTPS). It is safe to run repeatedly.
set -euo pipefail

CONFIG_FILE="${CONFIG_FILE:-${CONFIG_DIR:-/etc/agentcontrol}/config.yaml}"
[[ -f "$CONFIG_FILE" ]] || exit 0

log() { echo "==> $*"; }

key_value() {
  awk -v k="${1}:" '$1==k { $1=""; sub(/^[ \t]+/, ""); sub(/[ \t]+#.*$/, ""); gsub(/^["'"'"']|["'"'"']$/, ""); print; exit }' "$CONFIG_FILE"
}

has_key() {
  grep -qE "^${1}:" "$CONFIG_FILE"
}

set_key() {
  local key="$1" value="$2" tmp
  if has_key "$key"; then
    tmp="$(mktemp)"
    awk -v k="${key}:" -v v="$value" '$1==k { print k" "v; next } { print }' "$CONFIG_FILE" > "$tmp"
    cat "$tmp" > "$CONFIG_FILE"
    rm -f "$tmp"
  else
    printf '\n%s: %s\n' "$key" "$value" >> "$CONFIG_FILE"
  fi
}

port_free() {
  ! ss -ltn "sport = :$1" 2>/dev/null | grep -q LISTEN
}

pick_port() {
  local port
  for port in $(seq "$1" $(($1 + 60))); do
    if port_free "$port"; then
      echo "$port"
      return 0
    fi
  done
  return 1
}

PUBLIC_PORT="$(key_value port)"
PUBLIC_PORT="${PUBLIC_PORT:-30228}"
if ! [[ "$PUBLIC_PORT" =~ ^[0-9]+$ ]]; then
  log "config port is not a number — leaving Caddy disabled"
  exit 0
fi

if [[ -z "$(key_value internal_port)" ]]; then
  if [[ -n "$(key_value tls_cert_file)" || -n "$(key_value tls_key_file)" ]] || port_free 80; then
    INTERNAL_PORT="$(pick_port 30328)" || { log "no free loopback port for the panel"; exit 0; }
    HTTPS_INTERNAL_PORT="$(pick_port 30443)" || { log "no free loopback HTTPS port"; exit 0; }
    set_key internal_port "$INTERNAL_PORT"
    set_key bind_host "127.0.0.1"
    set_key https_internal_port "$HTTPS_INTERNAL_PORT"
    log "Enabled bundled Caddy: public ${PUBLIC_PORT} -> https ${HTTPS_INTERNAL_PORT} -> panel ${INTERNAL_PORT}"
  else
    log "Port 80 is busy and no tls_cert_file is set — keeping the panel on the public port"
    exit 0
  fi
fi

if [[ -z "$(key_value https_internal_port)" ]]; then
  HTTPS_INTERNAL_PORT="$(pick_port 30443)" || exit 0
  set_key https_internal_port "$HTTPS_INTERNAL_PORT"
fi

BIND_HOST="$(key_value bind_host)"
if [[ "$BIND_HOST" != "127.0.0.1" && "$BIND_HOST" != "localhost" ]]; then
  set_key bind_host "127.0.0.1"
fi
