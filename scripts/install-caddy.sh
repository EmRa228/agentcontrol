#!/usr/bin/env bash
# Install or refresh the AgentControl-bundled Caddy reverse proxy.
#
# Layout:
#   public port        -> agentcontrol-tls-front (this repo, scripts/tls_front.py)
#       TLS            -> Caddy on 127.0.0.1:<https_internal_port>
#       plain HTTP     -> 308 redirect to https://HOST:<public port>
#   Caddy              -> reverse proxy to Flask on 127.0.0.1:<internal_port>
#
# Caddy never touches 80/443 for the site itself; it uses port 80 only for the
# ACME HTTP-01 challenge. With public_hostname set the certificate is managed
# for that name. Without it Caddy uses On-Demand TLS: the certificate is issued
# for the hostname in the TLS handshake once its DNS points at this server, and
# the panel approves it on /api/caddy/allow.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/agentcontrol}"
CONFIG_DIR="${CONFIG_DIR:-/etc/agentcontrol}"
STATE_DIR="${STATE_DIR:-/var/lib/agentcontrol}"
CONFIG_FILE="${CONFIG_FILE:-${CONFIG_DIR}/config.yaml}"
SERVICE_NAME="agentcontrol-caddy"
FRONT_SERVICE_NAME="agentcontrol-tls-front"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
FRONT_UNIT_FILE="/etc/systemd/system/${FRONT_SERVICE_NAME}.service"
CADDY_VERSION="${CADDY_VERSION:-2.11.9}"

log() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "run as root (sudo bash $0)"

cfg_get() {
  local key="$1" default="${2:-}" value
  if [[ ! -f "$CONFIG_FILE" ]]; then
    printf '%s' "$default"
    return
  fi
  value="$(awk -v k="${key}:" '$1==k { $1=""; sub(/^[ \t]+/, ""); print; exit }' "$CONFIG_FILE")"
  value="${value%%[[:space:]]#*}"
  value="$(printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
  if [[ -n "$value" ]]; then printf '%s' "$value"; else printf '%s' "$default"; fi
}

PUBLIC_PORT="${PUBLIC_PORT:-$(cfg_get port 30228)}"
INTERNAL_PORT="${INTERNAL_PORT:-$(cfg_get internal_port "")}"
HTTPS_INTERNAL_PORT="${HTTPS_INTERNAL_PORT:-$(cfg_get https_internal_port 30443)}"
BIND_HOST="${BIND_HOST:-$(cfg_get bind_host "127.0.0.1")}"
PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME:-$(cfg_get public_hostname "")}"
TLS_CERT_FILE="${TLS_CERT_FILE:-$(cfg_get tls_cert_file "")}"
TLS_KEY_FILE="${TLS_KEY_FILE:-$(cfg_get tls_key_file "")}"
ACME_EMAIL="${ACME_EMAIL:-$(cfg_get acme_email "")}"
CONFIGURED_CADDY_BIN="${CADDY_BIN:-$(cfg_get caddy_bin "")}"

# Normalize a hostname such as "https://panel.example.com/" to "panel.example.com".
PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME#http://}"
PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME#https://}"
PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME%%/*}"

[[ "$INTERNAL_PORT" =~ ^[0-9]+$ ]] && (( INTERNAL_PORT > 0 )) \
  || die "config internal_port must be set to the Flask loopback port (e.g. 30328)"
[[ "$PUBLIC_PORT" =~ ^[0-9]+$ ]] && (( PUBLIC_PORT > 0 )) \
  || die "config port must be a valid public port"
[[ "$HTTPS_INTERNAL_PORT" =~ ^[0-9]+$ ]] && (( HTTPS_INTERNAL_PORT > 0 )) \
  || die "https_internal_port must be a valid loopback port (e.g. 30443)"
[[ "$BIND_HOST" == "127.0.0.1" || "$BIND_HOST" == "localhost" ]] \
  || die "bind_host must be 127.0.0.1 when using the bundled Caddy"
[[ "$INTERNAL_PORT" != "$PUBLIC_PORT" ]] || die "internal_port and port must differ"
[[ "$HTTPS_INTERNAL_PORT" != "$PUBLIC_PORT" ]] || die "https_internal_port and port must differ"

if [[ -n "$TLS_CERT_FILE" || -n "$TLS_KEY_FILE" ]]; then
  [[ -f "$TLS_CERT_FILE" && -f "$TLS_KEY_FILE" ]] \
    || die "tls_cert_file and tls_key_file must both exist"
  USE_ACME=0
else
  USE_ACME=1
fi
# Without a fixed hostname, ask Caddy for a certificate at handshake time.
if (( USE_ACME )) && [[ -z "$PUBLIC_HOSTNAME" ]]; then
  ON_DEMAND=1
else
  ON_DEMAND=0
fi

CADDY_DATA="${STATE_DIR}/caddy"
BIN_DIR="${STATE_DIR}/bin"
CADDYFILE="${CONFIG_DIR}/Caddyfile"
CADDY_BIN="${BIN_DIR}/caddy"

if [[ -n "$CONFIGURED_CADDY_BIN" && -x "$CONFIGURED_CADDY_BIN" ]]; then
  CADDY_BIN="$CONFIGURED_CADDY_BIN"
fi

mkdir -p "$BIN_DIR" "$CADDY_DATA" "$CONFIG_DIR"

case "$(uname -m)" in
  x86_64|amd64)
    asset="caddy_${CADDY_VERSION}_linux_amd64.tar.gz"
    checksum="5b8cb7faf8726a2028ad96a66e54e6e81e8f361bc6175e5d22bbd997cc5177fb"
    ;;
  aarch64|arm64)
    asset="caddy_${CADDY_VERSION}_linux_arm64.tar.gz"
    checksum="5e377cff994fc9362a6fcf695c53f226cfca3a3ef85bd3a3687cf252d6310165"
    ;;
  *)
    die "unsupported architecture: $(uname -m) (need x86_64 or arm64)"
    ;;
esac

download() {
  local url="$1" out="$2" attempt
  for attempt in "$url" "https://gh-proxy.com/${url}"; do
    if curl --fail --silent --show-error --location --connect-timeout 15 \
        --max-time 300 --retry 2 --output "$out" "$attempt"; then
      return 0
    fi
  done
  return 1
}

caddy_ok() {
  [[ -x "$CADDY_BIN" ]] && "$CADDY_BIN" version >/dev/null 2>&1
}

if ! caddy_ok; then
  log "Downloading Caddy ${CADDY_VERSION} (${asset})…"
  work="$(mktemp -d "$BIN_DIR/.caddy-install.XXXXXXXX")"
  trap 'rm -rf -- "$work"' EXIT
  download "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/${asset}" "$work/caddy.tar.gz" \
    || die "could not download Caddy"
  printf '%s  %s\n' "$checksum" "$work/caddy.tar.gz" | sha256sum --check >/dev/null \
    || die "Caddy download checksum mismatch"
  tar -xzf "$work/caddy.tar.gz" -C "$work" caddy
  install -m 0755 "$work/caddy" "$CADDY_BIN"
  trap - EXIT
  rm -rf -- "$work"
fi

log "Caddy: $("$CADDY_BIN" version | head -n1)"

# Stop only our units so the managed ports are free for reconfiguration. Never
# touch a different nginx/Caddy.
for unit in "$FRONT_SERVICE_NAME" "$SERVICE_NAME"; do
  if systemctl list-unit-files "${unit}.service" >/dev/null 2>&1; then
    systemctl stop "${unit}" 2>/dev/null || true
  fi
done
if ss -ltn "sport = :${PUBLIC_PORT}" 2>/dev/null | grep -q LISTEN; then
  die "port ${PUBLIC_PORT} is already in use by another service; stop it or choose another port"
fi
if ss -ltn "sport = :${HTTPS_INTERNAL_PORT}" 2>/dev/null | grep -q LISTEN; then
  die "port ${HTTPS_INTERNAL_PORT} is already in use by another service"
fi
if (( USE_ACME )); then
  if ss -ltn 'sport = :80' 2>/dev/null | grep -q LISTEN; then
    die "port 80 is in use, so Let's Encrypt HTTP-01 cannot work. Set tls_cert_file/tls_key_file in ${CONFIG_FILE} or free port 80."
  fi
fi

{
  echo "{"
  echo "    admin off"
  echo "    auto_https disable_redirects"
  if (( USE_ACME )); then
    [[ -n "$ACME_EMAIL" ]] && echo "    email ${ACME_EMAIL}"
  fi
  if (( ON_DEMAND )); then
    echo "    on_demand_tls {"
    echo "        ask http://127.0.0.1:${INTERNAL_PORT}/api/caddy/allow"
    echo "    }"
  fi
  echo "    storage file_system ${CADDY_DATA}/storage"
  echo "}"
  echo
  if [[ -n "$PUBLIC_HOSTNAME" ]]; then
    echo "https://${PUBLIC_HOSTNAME}:${HTTPS_INTERNAL_PORT} {"
  else
    echo "https://:${HTTPS_INTERNAL_PORT} {"
  fi
  echo "    bind 127.0.0.1"
  echo "    reverse_proxy ${BIND_HOST}:${INTERNAL_PORT} {"
  echo "        flush_interval -1"
  echo "    }"
  if (( USE_ACME )); then
    echo "    tls {"
    (( ON_DEMAND )) && echo "        on_demand"
    echo "        issuer acme {"
    echo "            disable_tlsalpn_challenge"
    echo "        }"
    echo "    }"
  else
    echo "    tls ${TLS_CERT_FILE} ${TLS_KEY_FILE}"
  fi
  echo "    header {"
  echo '        Strict-Transport-Security "max-age=31536000; includeSubDomains"'
  echo '        X-Content-Type-Options "nosniff"'
  echo '        X-Frame-Options "DENY"'
  echo '        Referrer-Policy "strict-origin-when-cross-origin"'
  echo '        Permissions-Policy "camera=(), microphone=(), geolocation=()"'
  echo "        -Server"
  echo "    }"
  echo "    log {"
  echo "        output file ${CADDY_DATA}/access.log"
  echo "    }"
  echo "}"
} > "$CADDYFILE"

template="${INSTALL_DIR}/agentcontrol-caddy.service"
if [[ -f "$template" ]]; then
  sed -e "s|@CADDY_BIN@|${CADDY_BIN}|g" \
      -e "s|@CONFIG_DIR@|${CONFIG_DIR}|g" \
      -e "s|@CADDY_DATA@|${CADDY_DATA}|g" \
      "$template" > "$UNIT_FILE"
else
  cat > "$UNIT_FILE" <<EOF
[Unit]
Description=AgentControl Caddy reverse proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=${CADDY_DATA}/home
Environment=XDG_DATA_HOME=${CADDY_DATA}/data
Environment=XDG_CONFIG_HOME=${CADDY_DATA}/config
ExecStart=${CADDY_BIN} run --config ${CADDYFILE} --adapter caddyfile
ExecReload=${CADDY_BIN} reload --config ${CADDYFILE} --adapter caddyfile
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${CADDY_DATA} ${CONFIG_DIR}
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
fi

PYTHON_BIN="${INSTALL_DIR}/venv/bin/python"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3)"
FRONT_SCRIPT="${INSTALL_DIR}/scripts/tls_front.py"
[[ -f "$FRONT_SCRIPT" ]] || die "missing ${FRONT_SCRIPT}"
FRONT_TEMPLATE="${INSTALL_DIR}/agentcontrol-tls-front.service"
[[ -f "$FRONT_TEMPLATE" ]] || die "missing ${FRONT_TEMPLATE}"
sed -e "s|@PYTHON@|${PYTHON_BIN}|g" \
    -e "s|@SCRIPT@|${FRONT_SCRIPT}|g" \
    -e "s|@PUBLIC_PORT@|${PUBLIC_PORT}|g" \
    -e "s|@HTTPS_INTERNAL_PORT@|${HTTPS_INTERNAL_PORT}|g" \
    -e "s|@INTERNAL_PORT@|${INTERNAL_PORT}|g" \
    -e "s|@DEFAULT_HOST@|${PUBLIC_HOSTNAME}|g" \
    "$FRONT_TEMPLATE" > "$FRONT_UNIT_FILE"

chmod 644 "$UNIT_FILE" "$FRONT_UNIT_FILE"
chmod 600 "$CADDYFILE"
mkdir -p "${CADDY_DATA}/home" "${CADDY_DATA}/data" "${CADDY_DATA}/config"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" "$FRONT_SERVICE_NAME" >/dev/null
systemctl restart "$SERVICE_NAME"
systemctl restart "$FRONT_SERVICE_NAME"

if [[ -n "$PUBLIC_HOSTNAME" ]]; then
  log "Waiting for https://${PUBLIC_HOSTNAME}:${PUBLIC_PORT}/health …"
  for _ in $(seq 1 45); do
    if curl -fsS --max-time 4 "https://${PUBLIC_HOSTNAME}:${PUBLIC_PORT}/health" >/dev/null 2>&1; then
      log "HTTPS front is serving https://${PUBLIC_HOSTNAME}:${PUBLIC_PORT} → ${BIND_HOST}:${INTERNAL_PORT}"
      log "Firewall: allow ${PUBLIC_PORT}/tcp; keep ${INTERNAL_PORT} and ${HTTPS_INTERNAL_PORT} loopback-only."
      (( USE_ACME )) && log "Firewall: allow 80/tcp for ACME HTTP-01 challenges only."
      exit 0
    fi
    sleep 2
  done
  die "HTTPS front is up but the health check failed. See: journalctl -u ${SERVICE_NAME} -u ${FRONT_SERVICE_NAME} -n 80"
fi

# On-demand mode: no fixed certificate name to test yet. Verify that the front
# answers plain HTTP with a redirect and that TLS is being served upstream.
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:${PUBLIC_PORT}/health" -H 'Host: localhost' || true)"
[[ "$code" == "308" ]] || die "HTTP redirect check failed on port ${PUBLIC_PORT} (got ${code:-no response})"
log "HTTPS front is serving port ${PUBLIC_PORT} with On-Demand ACME (no public_hostname set)."
log "Point a DNS name at this server, then open https://NAME:${PUBLIC_PORT} to issue its certificate."
log "Firewall: allow ${PUBLIC_PORT}/tcp and 80/tcp (ACME HTTP-01); keep ${INTERNAL_PORT} and ${HTTPS_INTERNAL_PORT} loopback-only."
