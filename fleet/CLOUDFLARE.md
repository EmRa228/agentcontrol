# Fleet on Cloudflare — limits and design rules

Fleet runs on **Cloudflare Workers** with **Workers KV**. Every feature must respect platform limits, especially on the **free tier**, or the dashboard will start returning **429** errors and break for all users.

**Official references (check for current numbers):**

- [Workers KV limits](https://developers.cloudflare.com/kv/platform/limits/)
- [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)
- [Workers known issues (fetch to IP)](https://developers.cloudflare.com/workers/platform/known-issues/#fetch-to-ip-addresses)

---

## Free tier limits (typical — verify in dashboard)

| Resource | Free tier (daily) | What counts |
|----------|-------------------|-------------|
| **KV reads** | 100,000 | Every `KV.get()` |
| **KV writes** | 1,000 | Every `KV.put()`, `KV.delete()`, list |
| **Worker requests** | 100,000 | Every HTTP request to the Worker |
| **CPU time** | 10 ms per request (free) | Per invocation |

Paid Workers ($5/mo minimum) raises KV to **10M reads** and **1M writes** per month — still worth designing efficiently.

---

## Why this matters for Fleet

Before v1.5.3, a **single open browser tab** could cause:

- Poll every **25s**
- Refresh **every registered server** on each poll
- **KV write** on every server refresh and on every `/api/fleet/cache` read

With 7 servers that is roughly **8 KV writes × ~3,500 polls/day ≈ 28,000 writes/day** — far above the **1,000/day** free write cap.

Symptom: Cloudflare email *"KV operations are nearing the daily cap"* and `429` from KV.

---

## Current design (v1.5.3+)

### Polling model (browser)

```
First load / empty state
  → GET /api/fleet/cache     (KV read only — no write)

Every 60s while tab visible
  → GET /api/fleet/server/:id   (rotate one server; persist=0 → no KV write)
  → UI merges returned snapshot in memory + localStorage

Tab hidden (document.visibilitychange)
  → polling stops; indicator shows "○ paused"

User clicks Refresh
  → GET /api/fleet/snapshot  (all servers; one KV write to fleet_snapshot_cache)

Every 10th poll (~10 min)
  → GET /api/servers         (detect newly added servers; persist=1 only for missing)
```

Constants in `fleet/public/index.html`: `POLL_MS = 60000`, `REGISTRY_POLL_EVERY = 10`.

### KV keys

| Key | Purpose | Write when |
|-----|---------|------------|
| `servers` | Registered servers (name, url, password, paused) | Add / delete / pause / touch |
| `fleet_snapshot_cache` | Last full fleet snapshot JSON | Full snapshot, add server, delete, pause, manual refresh |
| `recent_projects` | Cross-server “recent” touch metadata | Start / stop / touch project |
| `fleet_password` | Dashboard password (setup UI) | First-time setup |
| `fleet_ui_html` / `fleet_ui_version` | Cached UI from GitHub | CI deploy, manual “Update fleet UI” |

### API endpoints and KV impact

| Endpoint | KV reads (typical) | KV writes |
|----------|-------------------|-----------|
| `GET /api/fleet/cache` | snapshot + servers + recent | **none** |
| `GET /api/fleet/server/:id` | servers + snapshot + recent | only if `?persist=1` |
| `GET /api/fleet/snapshot` | servers + recent + N× proxy | **1** (full cache) |
| `POST /api/servers` | servers | servers + snapshot (persist) |
| `DELETE /api/servers/:id` | servers + snapshot | servers + snapshot |
| `POST /api/servers/:id/pause` | servers + snapshot | servers + optional snapshot |
| `POST /api/fleet/.../start\|stop` | servers + recent | recent (+ touch server list) |

---

## Design rules for contributors

**Do:**

- Prefer **read-only** cache endpoints for routine UI updates.
- **Rotate** expensive work (one server per poll), not all servers every tick.
- **Pause polling** when the browser tab is hidden.
- **Persist KV snapshot** only on user actions or explicit full refresh.
- Use **hostname** URLs for servers (grey-cloud DNS A record), never raw IPs ([error 1003](https://developers.cloudflare.com/workers/platform/known-issues/#fetch-to-ip-addresses)).
- Normalize activity timestamps: panel sends **Unix seconds**, Fleet KV uses **milliseconds** — always use `activityTs()` before sort/compare.
- Keep `localStorage` snapshot cache on the client to avoid redundant `/api/fleet/cache` calls after first load.

**Don't:**

- Call `KV.put()` on every `/api/fleet/cache` request.
- Refresh all servers on a fixed short interval in the background.
- Add per-request writes for `touchServer`, modal open, or hover.
- Store large blobs in KV beyond the UI HTML (snapshot JSON can grow with many servers — consider trimming `history` in cache if needed).
- Assume users have only one tab open — each tab polls independently.

**Before shipping a Fleet change, estimate:**

```
writes_per_day ≈ tabs_open × polls_per_day × writes_per_poll
                 + user_actions × writes_per_action
```

Target routine polling at **~0 writes/hour/tab**.

---

## Workers (non-KV) constraints

| Constraint | Fleet impact |
|------------|--------------|
| No `fetch()` to raw IP | Server URL must be `http://hostname:30228` |
| Request timeout | `proxyAgent` uses 45s timeout to panels |
| No long-lived connections | No SSE from Worker to browser for fleet state; use polling |
| Subrequest limits | Avoid fan-out of dozens of parallel panel calls per user request |

---

## Operations

### Hitting the KV cap

1. Limit open Fleet tabs.
2. Wait for UTC midnight reset (Cloudflare emails include reset time).
3. Deploy latest fleet (v1.5.3+) if still on old polling.
4. Upgrade to Workers Paid if you genuinely need higher volume.

### Monitoring

Cloudflare Dashboard → **Workers & Pages** → your worker → **Metrics**  
Also watch KV namespace usage under **Workers KV**.

### CI and KV

GitHub Actions (`fleet-deploy.yml`) writes `fleet_ui_html` and `fleet_ui_version` on each fleet deploy — **2 writes per deploy**, not per user poll.

---

## Related docs

- [fleet/README.md](README.md) — install and user guide
- [AGENTS.md](../AGENTS.md) — agent context and repo map
