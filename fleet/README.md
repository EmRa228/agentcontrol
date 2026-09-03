# AgentControl Fleet (Cloudflare Workers)

One dashboard for all your AgentControl servers. No central VPS — runs on **Cloudflare Workers** + **KV**.

Each server stays as-is (public IP + port + panel password). You add them from the fleet UI.

## What you need

- A [Cloudflare account](https://dash.cloudflare.com/sign-up) (free tier is enough to start)
- [Node.js](https://nodejs.org/) 18+ on your laptop
- Each AgentControl server reachable at a **hostname** from the internet (not a raw IP — see below)

## Important: use hostname, not IP

Cloudflare Workers **cannot** `fetch()` direct IP addresses ([known limitation](https://developers.cloudflare.com/workers/platform/known-issues/#fetch-to-ip-addresses)).  
If you use `http://193.163.201.14:30228` you get **error 1003**.

**Fix:** In your Cloudflare DNS (e.g. `aksbaz.com`):

1. **Add record** → Type **A**
2. Name: `ac-tg-uk` (or any name)
3. IPv4: `193.163.201.14`
4. Proxy status: **DNS only** (grey cloud — not orange)
5. In fleet, use: `http://ac-tg-uk.aksbaz.com:30228`

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

### 3. Set the fleet login password

This protects the fleet dashboard (separate from each server’s password):

```bash
npx wrangler secret put FLEET_PASSWORD
```

Enter a strong password when prompted.

Docs: [https://developers.cloudflare.com/workers/configuration/secrets/](https://developers.cloudflare.com/workers/configuration/secrets/)

### 4. Deploy

```bash
npm run deploy
```

Wrangler prints a `*.workers.dev` URL, for example:

`https://agentcontrol-fleet.your-subdomain.workers.dev`

Docs: [https://developers.cloudflare.com/workers/get-started/guide/#7-deploy-your-project](https://developers.cloudflare.com/workers/get-started/guide/#7-deploy-your-project)

### 5. Custom subdomain (optional)

1. Cloudflare Dashboard → **Workers & Pages** → your worker → **Settings** → **Domains & Routes**
2. **Add** → **Custom domain** → e.g. `fleet.example.com`
3. DNS must be on Cloudflare (orange cloud)

Docs: [https://developers.cloudflare.com/workers/configuration/routing/custom-domains/](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)

### 6. Use the fleet

1. Open your fleet URL
2. Sign in with **FLEET_PASSWORD**
3. **Add server**:
   - **Name**: anything (e.g. `uk-prod`)
   - **URL**: `http://193.163.201.14:30228`
   - **Panel password**: same password you use on that server’s AgentControl UI
4. Expand a server card → **Start** / **Stop** projects

The fleet UI polls `/api/fleet/snapshot` every **25 seconds** while the browser tab is **visible**. When the tab is in the background, polling stops to save Cloudflare Worker requests. The **Refresh** button fetches an immediate snapshot.

Version is shown in the header (`fleet/version.json`, also at `/version.json` and `GET /api/version`).

## Local development

```bash
# Set secrets for local dev (once)
npx wrangler secret put FLEET_PASSWORD

# Run locally
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

- Server panel passwords are stored in **KV** (only your Worker can read them).
- Use a strong **FLEET_PASSWORD** and HTTPS (workers.dev or custom domain).
- Each AgentControl server should use its own panel password.
- Fleet Worker calls your servers over **HTTP** — use only on trusted networks or add HTTPS to AgentControl later.

## Auto-deploy with GitHub Actions (no local `wrangler deploy`)

After a one-time setup, every push to `main` that changes `fleet/src/` (or related files) deploys the Worker automatically.

### Step 1 — Cloudflare API token

1. Open [Cloudflare API Tokens](https://dash.cloudflare.com/profile/api-tokens)
2. **Create Token** → **Edit Cloudflare Workers** template (or custom with **Account → Workers Scripts → Edit**)
3. Copy the token (shown once)

### Step 2 — Cloudflare Account ID

1. Open [Cloudflare Dashboard](https://dash.cloudflare.com/)
2. Select your account → **Workers & Pages** (or any zone)
3. Right sidebar → **Account ID** — copy it

### Step 3 — KV namespace ID

If `fleet/wrangler.jsonc` still contains `REPLACE_WITH_KV_NAMESPACE_ID`:

```bash
cd fleet
npx wrangler kv namespace list
```

Copy the `id` for the namespace bound as `KV` (or create one with `npx wrangler kv namespace create KV`).

Alternatively: Cloudflare Dashboard → **Workers & Pages** → **KV** → your namespace → copy **Namespace ID**.

### Step 4 — GitHub repository secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Secret name | Value |
|-------------|-------|
| `CLOUDFLARE_API_TOKEN` | Token from step 1 |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID from step 2 |
| `CLOUDFLARE_KV_NAMESPACE_ID` | KV id from step 3 (only if placeholder in wrangler.jsonc) |

### Step 5 — Verify

Push to `main` or run **Actions** → **Deploy Fleet Worker** → **Run workflow**.

- **Frontend only** (`fleet/public/index.html`): still use **Update fleet UI** in the dashboard (pulls from GitHub into KV).
- **Backend** (`fleet/src/index.ts`): deploys via this workflow on push.

`FLEET_PASSWORD` stays on Cloudflare (`wrangler secret put FLEET_PASSWORD`) — GitHub Actions does not need it; existing secrets are preserved on deploy.

## Troubleshooting

| Problem | Fix |
|--------|-----|
| `FLEET_PASSWORD secret is not set` | Run `npx wrangler secret put FLEET_PASSWORD` and redeploy |
| `cannot reach server` / `error 1003` | Use a **hostname** (A record, grey cloud), not raw IP — see above |
| `cannot reach server` (other) | Check firewall allows port 30228 from the internet |
| `wrong server password` | Use the same password as the single-server AgentControl login |
| `REPLACE_WITH_KV_NAMESPACE_ID` | Set `CLOUDFLARE_KV_NAMESPACE_ID` in GitHub Actions secrets, or paste KV id into `wrangler.jsonc` |
| GitHub Actions deploy failed | Check **Actions** tab logs; verify all three Cloudflare secrets |

## Architecture

```
Browser → fleet.yourdomain.com (Worker)
              ↓ HTTP + X-AgentControl-Auth
         http://server1:30228/api/...
         http://server2:30228/api/...
```

No Cloudflare Tunnel. No config changes on individual servers.
