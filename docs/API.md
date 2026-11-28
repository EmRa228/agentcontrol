# API reference

Panel routes are Flask; Fleet routes are Cloudflare Worker paths in
`fleet/src/index.ts`. Keep this table in sync with `app.py` — a new endpoint without
a row and a test is incomplete.

## Panel auth

- Browser: cookie `agentcontrol_session` (server-side session).
- Automation/Fleet: scoped token in `X-AgentControl-Token` (`read` < `ops` <
  `admin`), or the panel password in `X-AgentControl-Auth` / Bearer (admin).
- Every route is also available under `/api/v1/…`.
- Open routes below work without either. Everything else under `/api/` returns
  `401 {"error":"unauthorized"}`, or `403 {"error":"setup_required"}` until a panel
  password exists. First-run setup from a non-loopback address also needs
  `/etc/agentcontrol/setup-token` via `?setup=…` or `setup_token` in the body.
- `/oc/*` additionally rejects cross-origin requests (`Origin` must match `Host`).

```bash
# Create a token in Settings -> API tokens (shown once), then:
TOKEN='ac_...'
curl -s -H "X-AgentControl-Token: $TOKEN" \
  http://127.0.0.1:30328/api/folders
```

## Panel endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | open | HTML gate / panel |
| GET | `/health` | open | `{ok, component, version}` |
| GET | `/version.json`, `/api/version` | session/header | panel version |
| GET | `/connectivity-test`, `/connectivity-test-minimal.html` | open | network diagnostics |
| POST | `/api/auth/login` | open | password + optional `totp`; sets cookie |
| POST | `/api/auth/logout` | open | revokes current session |
| GET | `/api/auth/status` | open | auth/api-key/TOTP readiness |
| GET | `/api/auth/sessions` | session/header | count + TTL |
| POST | `/api/auth/sessions/revoke-all` | session/header | `{keep_current?: true}` |
| GET | `/api/setup/status` | open | first-run flags |
| POST | `/api/setup/password` | open until set | create panel password |
| POST | `/api/setup/api-key` | open until set | verify + store Cursor key |
| GET | `/api/models` | session/header | `agent --list-models` |
| GET | `/api/folders` | session/header | projects + worker state |
| POST | `/api/start/<name>` | session/header | start worker |
| POST | `/api/restart/<name>` | session/header | stop + start |
| POST | `/api/stop/<name>` | session/header | SIGTERM process group |
| POST | `/api/hide/<name>` | session/header | write `.agentcontrol-ignore` |
| POST | `/api/projects/touch/<name>` | session/header | bump recent activity |
| GET | `/api/ready/<name>` | session/header | readiness probe |
| GET | `/api/projects/<name>/details` | session/header | git branch/dirty/ahead-behind + worker details |
| GET | `/api/logs/<name>` | session/header | `?tail=&bytes=&q=` worker log tail |
| GET | `/api/logs/<name>/download` | session/header | download worker log |
| POST | `/api/hide/<name>`, `/api/unhide/<name>` | session/header | hide/restore a project |
| GET | `/api/hidden` | session/header | hidden projects |
| GET | `/api/status` | session/header | workers + effective config |
| GET | `/api/health` | session/header | health snapshot/checks |
| GET | `/api/health/history?days=` | session/header | persisted health samples |
| GET | `/api/incidents` | session/header | open/resolved incidents |
| GET | `/api/audit`, `/api/audit/export.csv` | admin | audit trail |
| GET/POST | `/api/tokens` | admin | list/create scoped tokens |
| POST | `/api/tokens/<id>/revoke` | admin | revoke token |
| GET/POST | `/api/notifications` | admin | alert config (secrets redacted) |
| POST | `/api/notifications/test` | admin | `{channel}` test send |
| GET | `/api/agent/status`, `POST /api/agent/update` | admin | agent CLI version/update |
| GET | `/api/opencode/versions` | session/header | installed/web/pinned/latest |
| POST | `/api/opencode/update`, `/api/opencode/restart` | admin | run update pipeline / restart serve |
| GET | `/api/openapi.json` | open | route list/schema |
| GET | `/api/public/status`, `/status` | open | read-only status page |
| GET | `/metrics` | token or loopback | Prometheus text (public if `metrics_public`) |
| GET | `/api/system` | session/header | CPU/RAM/disk/network snapshot |
| GET | `/api/system/history` | session/header | in-memory metrics points |
| GET | `/api/settings` | session/header | panel settings |
| GET/POST | `/api/settings/bridge` | session/header | Cursor edge bridge |
| POST | `/api/settings/bridge/test` | session/header | test Cursor connectivity |
| POST | `/api/settings/model` | session/header | default model |
| GET | `/api/xray/client`, `/api/xray/status` | session/header | xray settings/status |
| POST | `/api/xray/client`, `/toggle`, `/import`, `/parse`, `/test` | session/header | manage xray proxy |
| GET/POST | `/api/proxy/pool` | session/header | pool settings |
| POST | `/api/proxy/pool/{subscription,link,refresh,test,ensure,select}` | session/header | pool actions |
| GET/POST | `/api/settings/opencode` | session/header | provider keys, remote URL |
| POST | `/api/settings/opencode/remote/refresh` | session/header | refresh console URL |
| GET | `/api/opencode/status` | session/header | serve + install state |
| POST | `/api/opencode/ensure` | session/header | start serve |
| GET/POST | `/api/opencode/install` | session/header | async install/build |
| POST | `/api/opencode/open/<name>` | session/header | ensure serve + launch path |
| GET | `/api/settings/totp` | session/header | TOTP status |
| POST | `/api/settings/totp/{begin,enable,rotate,disable}` | session/header | TOTP lifecycle |
| GET | `/api/update/check` | session/header | compare with GitHub version |
| GET | `/api/update/status` | session/header | local/remote + log tail |
| POST | `/api/update/apply` | session/header | detached panel update |
| GET | `/api/fleet/bundle` | session/header | trimmed payload for Fleet |
| GET | `/api/fleet/sso?token=` | **open** (single-use) | consume launch token |
| GET | `/api/caddy/allow?domain=` | loopback only | On-Demand TLS approval |
| GET | `/oc`, `/oc/`, `/oc/<path>` | session + same-origin | OpenCode SPA/API/SSE |
| WS | `/oc/pty/<id>/connect` | session + same-origin | terminal |
| WS | `/oc/api/pty/<id>/connect` | session + same-origin | terminal |
| WS | `/oc/api/experimental/persistent-pty/<id>/connect` | session + same-origin | terminal |

Notes:

- `<name>` is a direct child folder name; `/` and `.`/`..` are rejected.
- Errors are `{"error": "message"}` with 4xx/5xx; OpenCode install uses
  `409 {"status":"install_required"}`.
- `/api/fleet/sso` token TTL is 180 s and single use; when TOTP is enabled the
  endpoint redirects to `/` instead of creating a session.

## Fleet endpoints (Worker)

Auth: HttpOnly `fleet_session` cookie after `POST /api/login`, or
`X-Fleet-Password` for CI/automation. Passwords are stored as PBKDF2 hashes; server
credentials are AES-GCM encrypted when `FLEET_ENC_KEY` is set. `GET` responses never
include passwords.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/version` | open; active Fleet UI version from KV |
| GET | `/api/update/check` | open; compare local vs GitHub |
| POST | `/api/login` | open; `{password}` → session cookie |
| POST | `/api/logout` | open; clears the session cookie |
| GET | `/api/setup/status` | open; first-run flag |
| POST | `/api/setup` | open until configured; create fleet password |
| POST | `/api/update/apply` | fetch and activate new UI from GitHub into KV |
| GET/POST | `/api/servers` | list / add (probes `/api/system`) |
| DELETE | `/api/servers/:id` | remove + drop snapshot |
| POST | `/api/servers/:id/pause` | toggle paused (skipped by polling/rollouts) |
| POST | `/api/servers/touch` | bump `lastAccessedAt` |
| GET | `/api/fleet/snapshot` | refresh all + persist cache |
| GET | `/api/fleet/cache` | cached snapshot, no writes |
| GET | `/api/fleet/server/:id?persist=1` | refresh one server |
| POST | `/api/fleet/update-all` | `/api/update/apply` on every non-paused server |
| POST | `/api/fleet/proxy/push` | push proxy settings to all panels |
| POST | `/api/fleet/:sid/{start,restart,stop,touch}/:project` | project actions |
| GET | `/api/fleet/:sid/ready/:project` | readiness |
| GET/POST | `/api/fleet/:sid/opencode-install` | install OpenCode |
| POST | `/api/fleet/:sid/opencode/:project` | open OpenCode (returns `fleet_opencode_url`) |
| GET/POST | `/api/notifications` | Fleet alert config (webhook/telegram, threshold) |
| POST | `/api/notifications/test` | send a test alert |
| GET | `/api/fleet/health/:id` | hourly health history samples |

Static: `/`, `/index.html`, `/version.json` from `fleet/public`.

## Error contract

- Panel: JSON `{"error": "..."}`. Fleet transparently forwards upstream status/body
  and adds `fleet_opencode_url` / `fleet_panel_url` for OpenCode launches.
- No versioned API prefix and no OpenAPI schema; treat these paths as the contract
  and update this file with any change.
