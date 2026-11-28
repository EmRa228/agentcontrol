# AgentControl Fleet (Cloudflare Workers)

One dashboard for all your AgentControl servers. No central VPS — runs on **Cloudflare Workers** + **KV**.

Each server stays as-is (public IP + port + panel password). You add them from the fleet UI.

## What you need

- A [Cloudflare account](https://dash.cloudflare.com/sign-up) (free tier is enough to start)
- [Node.js](https://nodejs.org/) 22+ on your laptop (Wrangler 4.x requirement)
- Each AgentControl server reachable at a **hostname** from the internet (not a raw IP — see below)

## Important: use hostname, not IP

Cloudflare Workers **cannot** `fetch()` direct IP addresses ([known limitation](https://developers.cloudflare.com/workers/platform/known-issues/#fetch-to-ip-addresses)).  
If you use `http://193.163.201.14:30228` you get **error 1003**.

**Fix:** In your Cloudflare DNS (e.g. `aksbaz.com`):

1. **Add record** → Type **A**
2. Name: `ac-tg-uk` (or any name)
3. IPv4: `193.163.201.14`
4. Proxy status: **DNS only** (grey cloud — not orange)
5. In fleet, use: `https://ac-tg-uk.aksbaz.com:30228`

Port `30228` only works with grey cloud. Orange cloud proxies ports 80/443 only.

## Quick install (~5 minutes)

### 1. Install Wrangler and log in

```bash
cd fleet
npm install
npx wrangler login
```

This opens the browser and links Wrangler to your Cloudflare account.  
Docs: [https://developers.cloudflare.com/workers/wrangler/commands/#login](https://developers.cloudflare.com/workers/wrangler/commands/#login)

### 2. Create a KV namespace

```bash
npx wrangler kv namespace create KV
```

Copy the `id` from the output and paste it into `wrangler.jsonc`:

```jsonc
"kv_namespaces": [
  {
    "binding": "KV",
    "id": "paste-your-id-here"
  }
]
```

Docs: [https://developers.cloudflare.com/kv/get-started/](https://developers.cloudflare.com/kv/get-started/)

### 3. Deploy

```bash
npm run deploy
```

Wrangler prints a `*.workers.dev` URL, for example:

`https://agentcontrol-fleet.your-subdomain.workers.dev`

Docs: [https://developers.cloudflare.com/workers/get-started/guide/#7-deploy-your-project](https://developers.cloudflare.com/workers/get-started/guide/#7-deploy-your-project)

### 4. First visit — choose a password

Open your fleet URL. On first visit you see a **setup** screen to choose the dashboard password (stored as a PBKDF2 hash in KV). Sign-in creates an HttpOnly session cookie; the password is no longer kept in `localStorage`.

No `wrangler secret put FLEET_PASSWORD` is required for a fresh install.

**Optional:** `wrangler secret put FLEET_PASSWORD` sets a fallback password in the Worker environment. KV (from the setup UI) takes precedence once set. Useful for local `wrangler dev` or if you prefer managing the password only via Cloudflare secrets.

### 5. Custom subdomain (optional)

1. Cloudflare Dashboard → **Workers & Pages** → your worker → **Settings** → **Domains & Routes**
2. **Add** → **Custom domain** → e.g. `fleet.example.com`
3. DNS must be on Cloudflare (orange cloud)

Docs: [https://developers.cloudflare.com/workers/configuration/routing/custom-domains/](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)

### 6. Use the fleet

1. Open your fleet URL
2. Sign in with the password you chose at setup
3. **Add server**:
   - **Name**: anything (e.g. `uk-prod`)
   - **URL**: `https://ac-uk.example.com:30228` (hostname, grey-cloud DNS)
   - **Panel password**: a scoped API token (`ac_…`, recommended) or the panel password
4. Expand a server card → **Start** / **Stop** projects
5. **Alerts** configures down/recovery notifications (webhook + Telegram) with a
   failure threshold and severity; a cron checks every 5 minutes.

The fleet UI keeps data fresh with **low KV usage** (important on Cloudflare free tier):

1. On first load: `GET /api/fleet/cache` (read-only)
2. Every **60 seconds** while the tab is **visible**: refresh **one** server (`GET /api/fleet/server/:id`, no KV write)
3. When the tab is in the background: polling **stops** (live indicator shows `○ paused`)
4. **Refresh** button: full snapshot of all servers (`GET /api/fleet/snapshot`, one KV write)

**Do not** open many Fleet tabs at once — each tab polls independently.

For limits, architecture, and contributor rules, see **[CLOUDFLARE.md](CLOUDFLARE.md)**.

Version is shown in the header (`fleet/version.json`, also at `/version.json` and `GET /api/version`).

## Local development

```bash
# Optional: set a dev password (or use the setup screen on first local visit)
npx wrangler secret put FLEET_PASSWORD

npm run dev
```

Open the URL Wrangler prints (usually `http://localhost:8787`).

Docs: [https://developers.cloudflare.com/workers/wrangler/commands/#dev](https://developers.cloudflare.com/workers/wrangler/commands/#dev)

## Update after git pull

```bash
cd fleet
npm install   # if package.json changed
npm run deploy
```

## Security notes

- The dashboard password is stored as a **PBKDF2 hash**; browsers use an HttpOnly
  session cookie. Login is limited to 10 failures per IP / 5 minutes.
- Server credentials are stored in **KV**. Set `FLEET_ENC_KEY` (base64 32 bytes) to
  encrypt them with AES-GCM:
  `npx wrangler secret put FLEET_ENC_KEY`
- Prefer a scoped **API token** (`read`/`ops`/`admin`) per server instead of the
  panel password. Fleet sends it as `X-AgentControl-Token`.
- Choose a strong fleet password at first visit and use HTTPS (workers.dev or a
  custom domain). Put Cloudflare Access in front for an extra layer.
- Per-server health history and down/recovery alerts are sampled hourly; estimate KV
  writes before lowering the cron interval.

## Auto-deploy with GitHub Actions (no local `wrangler deploy`)

After a one-time setup, pushes to `main` automatically:

| What changed | What CI does |
|--------------|--------------|
| `fleet/**` | Deploy Worker + sync Fleet UI to KV |
| Panel files (`app.py`, `templates/`, …) | Call Fleet → update all registered servers |

**No manual “Update fleet UI”** — CI writes HTML to KV after each fleet deploy. The Worker also refreshes KV from GitHub when the bundled version is newer.

### Step 1 — Cloudflare API token

1. Open [Cloudflare API Tokens](https://dash.cloudflare.com/profile/api-tokens)
2. **Create Token** → **Edit Cloudflare Workers** template
3. Copy the token

### Step 2 — Cloudflare Account ID

Dashboard → **Workers & Pages** → sidebar **Account ID**

### Step 3 — GitHub secrets (required for deploy)

Repo → **Settings** → **Secrets and variables** → **Actions** → **Secrets**:

| Secret | Required | Purpose |
|--------|----------|---------|
| `CLOUDFLARE_API_TOKEN` | yes | Deploy Worker + KV sync |
| `CLOUDFLARE_ACCOUNT_ID` | yes | Deploy Worker |

KV namespace ID is resolved automatically from Cloudflare and committed to `wrangler.jsonc` on first deploy — **no `CLOUDFLARE_KV_NAMESPACE_ID` secret needed**.

### Step 4 — Optional: automatic panel rollout

Only needed if you want CI to call your Fleet hub and run `panel-update.sh` on **every registered server** when panel code changes (`app.py`, `templates/`, etc.).

| Setting | Where | When set | When not set |
|---------|-------|----------|--------------|
| `FLEET_URL` | **Variables** → Actions | CI knows your hub URL (e.g. `https://fleet.example.com`) | Panel rollout step is **skipped** — deploy Fleet + sync UI still works |
| `FLEET_PASSWORD` | **Secrets** → Actions | CI can authenticate to `/api/fleet/update-all` | Panel rollout step is **skipped** |

Use the **same password** you chose in the Fleet setup UI. This is independent of Cloudflare `wrangler secret put FLEET_PASSWORD` (KV password from setup is what matters for login).

For a manual rollout, push the panel release to `main`, open Fleet and choose **Update all panels**. Confirm to send an update request to every unpaused server. Fleet reports each response. An accepted request starts an asynchronous update on that host. Check the panel's `/health` for the installed version and `/var/lib/agentcontrol/update.log` for details. Panel restarts can stop active Cursor workers.

### Step 5 — Verify

Push to `main` or **Actions** → **Deploy Fleet Worker** → **Run workflow**.

## Troubleshooting

| Problem | Fix |
|--------|-----|
| Setup screen on every visit | Password not saved — check KV binding in `wrangler.jsonc` |
| `cannot reach server` / `error 1003` | Use a **hostname** (A record, grey cloud), not raw IP — see above |
| `cannot reach server` (other) | Check firewall allows port 30228 from the internet |
| `wrong server password` | Use the same password as the single-server AgentControl login |
| `REPLACE_WITH_KV_NAMESPACE_ID` | CI resolves KV id automatically on first deploy |
| Panel servers not updating after git push | Set GitHub **variable** `FLEET_URL` and **secret** `FLEET_PASSWORD` (setup password) |
| GitHub Actions deploy failed | Check **Actions** tab logs; verify `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` |
| Email: KV nearing daily cap / HTTP 429 | Too many KV writes — deploy latest fleet; close extra tabs; read [CLOUDFLARE.md](CLOUDFLARE.md) |

## Architecture

```
Browser → fleet.yourdomain.com (Worker)
              ↓ HTTP + X-AgentControl-Auth
         https://server1:30228/api/...
         https://server2:30228/api/...
```

No Cloudflare Tunnel. No config changes on individual servers.

**Platform constraints:** Workers cannot fetch raw IPs; KV has daily read/write caps on the free tier. See **[CLOUDFLARE.md](CLOUDFLARE.md)** for how Fleet is designed around these limits.
