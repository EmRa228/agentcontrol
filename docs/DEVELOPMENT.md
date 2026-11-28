# Development

Python panel + static UI; Fleet is TypeScript on Cloudflare Workers. No bundler for
the panel — `templates/index.html` is one file with inline CSS/JS.

## Local panel

```bash
cd /opt/agentcontrol
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
PORT=18080 INTERNAL_PORT=0 BIND_HOST=127.0.0.1 \
STATE_DIR=/tmp/agentcontrol ./venv/bin/python app.py
```

- `ensure_host_panel()` refuses to start inside Docker. Tests mock that.
- Without `/etc/agentcontrol/config.yaml`, `config.example.yaml` is read (defaults).
- Use a throwaway `STATE_DIR` so you do not touch real sessions/workers.
- `PORT` forces the public port; `INTERNAL_PORT=0` makes Flask serve it directly.
- The panel is a Werkzeug dev server (`app.run`) even in production; do not add
  blocking calls to request handlers. WebSocket routes rely on Werkzeug 3.1 +
  `simple-websocket`.

## Tests

```bash
./venv/bin/python -m unittest discover -s tests          # 101 tests, ~3 s
./venv/bin/python -m unittest tests.test_panel_totp -v
bash tests/test_install_wizard_network.sh
```

Coverage: auth/API-key setup, sessions, TOTP, Argon2 passwords, scoped tokens,
login lockout, SQLite store, notifications config, TLS front redirects, Caddy
allow, host runtime guards, proxy pool/env, OpenCode panel/URL, worker git/data/log
parsing, install-wizard defaults, and the panel web routes (headers, CSP, v1
aliases, health, audit, OpenMetrics, OpenAPI).

**Add a test with every endpoint change.** Prefer `app.test_client()` for routes and
pure-function tests for helpers; mock subprocess/network. There is no coverage gate.

## Adding an endpoint

1. Add the route to the matching blueprint in `panel/routes/` (`auth`, `workers`,
   `opencode`, `proxy`, `settings`, `system`, `ops`). Globals are resolved live from
   `app.py` via `panel/routes/_live.py`, so helpers stay in `app.py`.
2. It is protected by `app.before_request` unless you add the **unprefixed** endpoint
   name to `open_endpoints` (justify why).
3. Validate `name` via `folder_path()` / existing helpers; never build shell strings
   from user input.
4. Return `{"error": "..."}` with a 4xx/5xx; keep payloads small for Fleet.
5. If it mutates worker state, call `reconcile_state()` and save.
6. Add a test and a row in `docs/API.md`. If it adds config/env, update
   `docs/CONFIG.md` and `config.example.yaml`.
7. User-visible change → bump all version files and update README/AGENTS if the
   behavior is described there.

## UI conventions

- `templates/index.html` holds markup; `static/panel.js` is an ES module. No inline
  `<script>`/`onclick`: buttons use `data-action` (optional `data-arg`) and the
  delegated dispatcher at the end of `panel.js`.
- CSP uses a per-request nonce; inline event handlers would be blocked. Keep
  dynamic markup on `data-action`, `data-*` attributes and delegated listeners.
- Projects list re-renders on a 2 s poll. Action buttons must use delegated
  handlers — inline `onclick` on fresh markup is fragile (and blocked by CSP).
- Modals set `data-modal-closable`; polling pauses while a modal is open.
- Settings are modals; long operations show the `oc-progress` / start-step UI.
- Themes are CSS variable overrides (`data-theme` on `<html>`): dark/light/nord.
- Browser checks: saved-session reload, `/oc/` assets (JS/CSS MIME), SSE
  `/oc/global/event`, terminal WebSocket, mobile width (30228 from a phone).

## Fleet

```bash
cd fleet
npm ci
npx wrangler dev            # local worker
npx wrangler deploy         # CI does this on main when fleet/** changes
```

- `wrangler.jsonc` holds the worker name and KV id; `fleet/version.json` is inlined
  at build by `wrangler` (see `src/index.ts` import).
- CI (`.github/workflows/fleet-deploy.yml`): deploys the Worker, syncs
  `fleet_ui_html` / `fleet_ui_version` into KV, and (if `FLEET_URL` +
  `FLEET_PASSWORD` are configured) triggers `/api/fleet/update-all` for panel changes.
- Any polling/caching/KV change must be estimated against `fleet/CLOUDFLARE.md`.
- Manual test: add a server, load cache, rotate a server, start/stop a project, open
  OpenCode, pause/resume, update all.

## Versioning & release

Four version locations must agree for a release:

- `version.json` (panel `/health`, update check)
- `fleet/version.json` and `fleet/public/version.json` (Fleet UI/update)
- `fleet/package.json` (metadata; not read at runtime)

`scripts/panel-update.sh` deploys `origin/main` verbatim, and Fleet pulls UI from
GitHub — so push first, then trigger rollouts.

Release checklist:

1. Tests pass; docs updated (`API`, `CONFIG`, `OPERATIONS` if behavior changed).
2. Bump all four versions; update `updated` date.
3. Commit + push to `main`.
4. Fleet: verify `/api/version` and UI; Panels: update one canary, check `/health`
   and `update.log`, then “Update all panels”.
5. Keep `README.md` user-facing; internal details belong under `docs/`.
