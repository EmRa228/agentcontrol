import fleetVersion from "../version.json";

export interface Env {
  KV: KVNamespace;
  ASSETS: Fetcher;
  FLEET_PASSWORD: string;
}

interface StoredServer {
  id: string;
  name: string;
  url: string;
  password: string;
  addedAt: number;
  lastAccessedAt?: number;
}

interface ServerRecord {
  servers: StoredServer[];
}

interface RecentProject {
  serverId: string;
  serverName: string;
  serverUrl: string;
  project: string;
  touchedAt: number;
  running?: boolean;
}

const KV_KEY = "servers";
const KV_UI_HTML = "fleet_ui_html";
const KV_UI_VERSION = "fleet_ui_version";
const KV_RECENT = "recent_projects";
const GITHUB_RAW = "https://raw.githubusercontent.com/EmRa228/agentcontrol/main/fleet";
const FLEET_VERSION = fleetVersion as { component: string; version: string; updated: string };

interface VersionInfo {
  component: string;
  version: string;
  updated: string;
}

function versionTuple(value: string): [number, number, number] {
  const parts = value.split(".").map((part) => parseInt(part, 10) || 0);
  while (parts.length < 3) parts.push(0);
  return [parts[0], parts[1], parts[2]];
}

function versionIsNewer(remote: string, local: string): boolean {
  const r = versionTuple(remote);
  const l = versionTuple(local);
  for (let i = 0; i < 3; i += 1) {
    if (r[i] > l[i]) return true;
    if (r[i] < l[i]) return false;
  }
  return false;
}

async function getActiveFleetVersion(kv: KVNamespace): Promise<VersionInfo> {
  const cached = await kv.get(KV_UI_VERSION);
  if (cached) {
    try {
      return JSON.parse(cached) as VersionInfo;
    } catch {
      /* fall through */
    }
  }
  return FLEET_VERSION;
}

async function fetchRemoteFleetVersion(): Promise<VersionInfo> {
  const res = await fetch(`${GITHUB_RAW}/version.json`, { signal: AbortSignal.timeout(12000) });
  if (!res.ok) throw new Error(`GitHub version fetch failed: HTTP ${res.status}`);
  return (await res.json()) as VersionInfo;
}

async function applyFleetUiUpdate(kv: KVNamespace): Promise<VersionInfo> {
  const [versionRes, htmlRes] = await Promise.all([
    fetch(`${GITHUB_RAW}/version.json`, { signal: AbortSignal.timeout(15000) }),
    fetch(`${GITHUB_RAW}/public/index.html`, { signal: AbortSignal.timeout(25000) }),
  ]);
  if (!versionRes.ok || !htmlRes.ok) {
    throw new Error("Failed to download fleet UI from GitHub");
  }
  const version = (await versionRes.json()) as VersionInfo;
  const html = await htmlRes.text();
  await kv.put(KV_UI_VERSION, JSON.stringify(version));
  await kv.put(KV_UI_HTML, html);
  return version;
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function isDirectIpHost(hostname: string): boolean {
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(hostname)) return true;
  if (hostname.startsWith("[") && hostname.endsWith("]")) return true;
  if (hostname.includes(":") && !hostname.includes(".")) return true;
  return false;
}

function directIpError(hostname: string): string {
  return (
    `Cloudflare Workers cannot call direct IP addresses (${hostname} — error 1003). ` +
    "Create a DNS A record in your zone (grey cloud / DNS only), e.g. " +
    "ac-tg-uk.aksbaz.com → 193.163.201.14, then use http://ac-tg-uk.aksbaz.com:30228"
  );
}

function normalizeUrl(raw: string): string {
  let url = raw.trim();
  if (!url) throw new Error("URL required");
  if (!/^https?:\/\//i.test(url)) url = `http://${url}`;
  const parsed = new URL(url);
  if (isDirectIpHost(parsed.hostname)) {
    throw new Error(directIpError(parsed.hostname));
  }
  return `${parsed.protocol}//${parsed.host}`.replace(/\/$/, "");
}

function fleetAuth(request: Request, env: Env): boolean {
  const header = request.headers.get("X-Fleet-Password") || "";
  const expected = env.FLEET_PASSWORD || "";
  if (!expected) return false;
  return header === expected;
}

async function readServers(kv: KVNamespace): Promise<StoredServer[]> {
  const raw = await kv.get(KV_KEY);
  if (!raw) return [];
  try {
    const data = JSON.parse(raw) as ServerRecord;
    return data.servers || [];
  } catch {
    return [];
  }
}

async function writeServers(kv: KVNamespace, servers: StoredServer[]): Promise<void> {
  const payload: ServerRecord = { servers };
  await kv.put(KV_KEY, JSON.stringify(payload));
}

async function readRecentProjects(kv: KVNamespace): Promise<RecentProject[]> {
  const raw = await kv.get(KV_RECENT);
  if (!raw) return [];
  try {
    return JSON.parse(raw) as RecentProject[];
  } catch {
    return [];
  }
}

async function writeRecentProjects(kv: KVNamespace, items: RecentProject[]): Promise<void> {
  await kv.put(KV_RECENT, JSON.stringify(items.slice(0, 200)));
}

async function touchRecentProject(
  kv: KVNamespace,
  server: StoredServer,
  project: string,
  running?: boolean,
): Promise<void> {
  const items = await readRecentProjects(kv);
  const now = Date.now();
  const filtered = items.filter((i) => !(i.serverId === server.id && i.project === project));
  filtered.unshift({
    serverId: server.id,
    serverName: server.name,
    serverUrl: server.url,
    project,
    touchedAt: now,
    running,
  });
  await writeRecentProjects(kv, filtered);
}

async function touchServerAccess(kv: KVNamespace, serverId: string): Promise<void> {
  const servers = await readServers(kv);
  const idx = servers.findIndex((s) => s.id === serverId);
  if (idx < 0) return;
  servers[idx].lastAccessedAt = Date.now();
  await writeServers(kv, servers);
}

async function proxyAgent(
  server: StoredServer,
  path: string,
  method = "GET",
  body?: unknown,
): Promise<Response> {
  const url = `${server.url.replace(/\/$/, "")}${path}`;
  const init: RequestInit = {
    method,
    headers: {
      "X-AgentControl-Auth": server.password,
      Accept: "application/json",
    },
    signal: AbortSignal.timeout(45000),
  };
  if (body !== undefined) {
    init.headers = { ...init.headers, "content-type": "application/json" };
    init.body = JSON.stringify(body);
  }
  return fetch(url, init);
}

async function readJsonSafe(res: Response): Promise<unknown> {
  const text = await res.text();
  if (text.includes("error code: 1003")) {
    return {
      error: "Cloudflare error 1003 — use a hostname, not an IP address",
      status: res.status,
      hint: "Add an A record (DNS only) in your zone and use http://name.aksbaz.com:30228",
    };
  }
  try {
    return JSON.parse(text);
  } catch {
    return { error: text || res.statusText, status: res.status };
  }
}

function publicServer(s: StoredServer) {
  return {
    id: s.id,
    name: s.name,
    url: s.url,
    addedAt: s.addedAt,
    lastAccessedAt: s.lastAccessedAt || s.addedAt,
  };
}

function sortSnapshotsByRecent<T extends { id: string; lastAccessedAt?: number }>(snapshots: T[]): T[] {
  return [...snapshots].sort((a, b) => (b.lastAccessedAt || 0) - (a.lastAccessedAt || 0));
}

function buildOverview(snapshots: Array<Record<string, unknown>>) {
  const alerts: Array<{ level: string; server: string; message: string }> = [];
  let online = 0;
  let offline = 0;
  let workers = 0;
  let proxyIssues = 0;

  for (const s of snapshots) {
    const name = String(s.name || s.id);
    if (s.online) {
      online += 1;
      const summary = s.summary as { workers?: number } | undefined;
      workers += summary?.workers || 0;
      const proxy = s.proxy as { enabled?: boolean; listening?: boolean } | undefined;
      if (proxy?.enabled && !proxy?.listening) {
        proxyIssues += 1;
        alerts.push({ level: "error", server: name, message: "Proxy enabled but not listening" });
      }
      const sys = s.system as { cpu?: { percent?: number }; memory?: { percent?: number }; disk_root?: { percent?: number } } | undefined;
      if ((sys?.cpu?.percent || 0) > 90) {
        alerts.push({ level: "warn", server: name, message: `High CPU: ${sys?.cpu?.percent}%` });
      }
      if ((sys?.memory?.percent || 0) > 90) {
        alerts.push({ level: "warn", server: name, message: `High RAM: ${sys?.memory?.percent}%` });
      }
      if ((sys?.disk_root?.percent || 0) > 90) {
        alerts.push({ level: "warn", server: name, message: `High disk: ${sys?.disk_root?.percent}%` });
      }
    } else {
      offline += 1;
      alerts.push({ level: "error", server: name, message: String(s.error || "Server offline") });
    }
  }

  return {
    ok: alerts.length === 0,
    online,
    offline,
    workers,
    proxyIssues,
    alerts,
    message: alerts.length ? `${alerts.length} issue(s) need attention` : "All servers look good",
  };
}

function aggregateRecentProjects(
  snapshots: Array<Record<string, unknown>>,
  kvRecent: RecentProject[],
  limit = 10,
): RecentProject[] {
  const fromSnapshots: RecentProject[] = [];
  for (const s of snapshots) {
    if (!s.online) continue;
    const folders = (s.folders || []) as Array<{
      name: string;
      running?: boolean;
      touched_at?: number;
      touched_relative?: string;
      mtime?: number;
    }>;
    for (const f of folders) {
      fromSnapshots.push({
        serverId: String(s.id),
        serverName: String(s.name),
        serverUrl: String(s.url),
        project: f.name,
        touchedAt: f.touched_at || f.mtime || 0,
        running: f.running,
      });
    }
  }
  const merged = new Map<string, RecentProject>();
  for (const item of [...kvRecent, ...fromSnapshots]) {
    const key = `${item.serverId}:${item.project}`;
    const existing = merged.get(key);
    if (!existing || item.touchedAt > existing.touchedAt) {
      merged.set(key, item);
    }
  }
  return [...merged.values()].sort((a, b) => b.touchedAt - a.touchedAt).slice(0, limit);
}

async function snapshotOneServer(server: StoredServer) {
  const base = {
    id: server.id,
    name: server.name,
    url: server.url,
    lastAccessedAt: server.lastAccessedAt || server.addedAt,
  };
  try {
    const bundleRes = await proxyAgent(server, "/api/fleet/bundle");
    if (bundleRes.status === 401) {
      return { ...base, online: false, error: "auth failed" };
    }
    if (!bundleRes.ok) {
      return {
        ...base,
        online: false,
        error: `HTTP ${bundleRes.status}`,
      };
    }
    const bundle = (await bundleRes.json()) as {
      system?: Record<string, unknown>;
      folders?: unknown[];
      history?: unknown[];
      version?: VersionInfo;
      proxy?: Record<string, unknown>;
    };
    const system = bundle.system || {};
    const folders = bundle.folders || [];
    const cpu = system.cpu as { percent?: number; load_percent?: number; cores?: number } | undefined;
    const memory = system.memory as {
      percent?: number;
      swap_percent?: number | null;
      swap_used_human?: string | null;
    } | undefined;
    const disk = system.disk_root as { percent?: number } | undefined;
    const panel = system.panel as { running_workers?: number; project_count?: number; version?: string } | undefined;
    const hostname = system.hostname as string | undefined;
    const ip = system.ip as string | undefined;
    return {
      ...base,
      online: true,
      version: bundle.version?.version || panel?.version || null,
      hostname: hostname || null,
      ip: ip || null,
      proxy: bundle.proxy || null,
      summary: {
        cpu: cpu?.percent ?? null,
        load_pct: cpu?.load_percent ?? null,
        cores: cpu?.cores ?? null,
        ram: memory?.percent ?? null,
        swap: memory?.swap_percent ?? null,
        disk: disk?.percent ?? null,
        workers: panel?.running_workers ?? 0,
        projects: panel?.project_count ?? folders.length,
      },
      system,
      folders,
      history: bundle.history || [],
    };
  } catch (e) {
    return { ...base, online: false, error: String(e) };
  }
}

async function buildFleetSnapshot(kv: KVNamespace) {
  const servers = await readServers(kv);
  const snapshots = await Promise.all(servers.map(snapshotOneServer));
  const sorted = sortSnapshotsByRecent(snapshots);
  const recentKv = await readRecentProjects(kv);
  const recentProjects = aggregateRecentProjects(sorted, recentKv, 10);
  const overview = buildOverview(sorted);
  return {
    servers: sorted,
    recentProjects,
    recentProjectsAll: aggregateRecentProjects(sorted, recentKv, 500),
    overview,
    at: Date.now(),
  };
}

async function pushProxyToServers(kv: KVNamespace, config: Record<string, unknown>) {
  const servers = await readServers(kv);
  const results = [];
  for (const server of servers) {
    try {
      const res = await proxyAgent(server, "/api/proxy/pool", "POST", config);
      results.push({
        server: server.name,
        id: server.id,
        ok: res.ok,
        status: res.status,
        body: await readJsonSafe(res),
      });
    } catch (e) {
      results.push({ server: server.name, id: server.id, ok: false, error: String(e) });
    }
  }
  return results;
}

async function updateAllPanels(kv: KVNamespace) {
  const servers = await readServers(kv);
  const results = [];
  for (const server of servers) {
    try {
      const res = await proxyAgent(server, "/api/update/apply", "POST");
      results.push({
        server: server.name,
        id: server.id,
        ok: res.ok || res.status === 202,
        status: res.status,
        body: await readJsonSafe(res),
      });
    } catch (e) {
      results.push({ server: server.name, id: server.id, ok: false, error: String(e) });
    }
  }
  return results;
}

async function handleApi(request: Request, env: Env, url: URL): Promise<Response> {
  const path = url.pathname;

  if (path === "/api/version" && request.method === "GET") {
    const version = await getActiveFleetVersion(env.KV);
    return json(version);
  }

  if (path === "/api/update/check" && request.method === "GET") {
    const local = await getActiveFleetVersion(env.KV);
    try {
      const remote = await fetchRemoteFleetVersion();
      return json({
        local,
        remote,
        update_available: versionIsNewer(remote.version, local.version),
      });
    } catch (e) {
      return json({
        local,
        remote: null,
        update_available: false,
        error: String(e),
      });
    }
  }

  if (path === "/api/login" && request.method === "POST") {
    const data = (await request.json().catch(() => ({}))) as { password?: string };
    if (!env.FLEET_PASSWORD) {
      return json({ error: "FLEET_PASSWORD secret is not set on the Worker" }, 503);
    }
    if ((data.password || "") === env.FLEET_PASSWORD) {
      return json({ ok: true });
    }
    return json({ error: "wrong password" }, 401);
  }

  if (!fleetAuth(request, env)) {
    return json({ error: "unauthorized" }, 401);
  }

  if (path === "/api/update/apply" && request.method === "POST") {
    try {
      const version = await applyFleetUiUpdate(env.KV);
      return json({ status: "updated", version });
    } catch (e) {
      return json({ error: String(e) }, 500);
    }
  }

  if (path === "/api/servers" && request.method === "GET") {
    const servers = await readServers(env.KV);
    return json({ servers: servers.map(publicServer) });
  }

  if (path === "/api/servers" && request.method === "POST") {
    const data = (await request.json()) as { name?: string; url?: string; password?: string };
    const name = (data.name || "").trim();
    const password = (data.password || "").trim();
    if (!name || !password) return json({ error: "name and password required" }, 400);
    let baseUrl: string;
    try {
      baseUrl = normalizeUrl(data.url || "");
    } catch (e) {
      return json({ error: String(e) }, 400);
    }

    const probe = await proxyAgent({ id: "", name, url: baseUrl, password, addedAt: 0 }, "/api/system");
    if (probe.status === 401) return json({ error: "wrong server password" }, 400);
    if (!probe.ok) {
      const err = await readJsonSafe(probe);
      return json({ error: "cannot reach server", detail: err }, 400);
    }

    const servers = await readServers(env.KV);
    const entry: StoredServer = {
      id: crypto.randomUUID(),
      name,
      url: baseUrl,
      password,
      addedAt: Date.now(),
      lastAccessedAt: Date.now(),
    };
    servers.push(entry);
    await writeServers(env.KV, servers);
    return json({ server: publicServer(entry) });
  }

  const deleteMatch = path.match(/^\/api\/servers\/([^/]+)$/);
  if (deleteMatch && request.method === "DELETE") {
    const id = deleteMatch[1];
    const servers = (await readServers(env.KV)).filter((s) => s.id !== id);
    await writeServers(env.KV, servers);
    return json({ ok: true });
  }

  if (path === "/api/servers/touch" && request.method === "POST") {
    const data = (await request.json().catch(() => ({}))) as { serverId?: string };
    const serverId = String(data.serverId || "").trim();
    if (!serverId) return json({ error: "serverId required" }, 400);
    await touchServerAccess(env.KV, serverId);
    return json({ ok: true });
  }

  if (path === "/api/fleet/snapshot" && request.method === "GET") {
    return json(await buildFleetSnapshot(env.KV));
  }

  if (path === "/api/fleet/update-all" && request.method === "POST") {
    const results = await updateAllPanels(env.KV);
    return json({ ok: true, results });
  }

  if (path === "/api/fleet/proxy/push" && request.method === "POST") {
    const data = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const results = await pushProxyToServers(env.KV, data);
    return json({ ok: results.every((r) => r.ok), results });
  }

  const startMatch = path.match(/^\/api\/fleet\/([^/]+)\/start\/([^/]+)$/);
  if (startMatch && request.method === "POST") {
    const [, serverId, project] = startMatch;
    const server = (await readServers(env.KV)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    await touchServerAccess(env.KV, serverId);
    await touchRecentProject(env.KV, server, decodeURIComponent(project), true);
    const res = await proxyAgent(server, `/api/start/${encodeURIComponent(project)}`, "POST");
    return json(await readJsonSafe(res), res.status);
  }

  const stopMatch = path.match(/^\/api\/fleet\/([^/]+)\/stop\/([^/]+)$/);
  if (stopMatch && request.method === "POST") {
    const [, serverId, project] = stopMatch;
    const server = (await readServers(env.KV)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    await touchServerAccess(env.KV, serverId);
    await touchRecentProject(env.KV, server, decodeURIComponent(project), false);
    const res = await proxyAgent(server, `/api/stop/${encodeURIComponent(project)}`, "POST");
    return json(await readJsonSafe(res), res.status);
  }

  const touchMatch = path.match(/^\/api\/fleet\/([^/]+)\/touch\/([^/]+)$/);
  if (touchMatch && request.method === "POST") {
    const [, serverId, project] = touchMatch;
    const server = (await readServers(env.KV)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    await touchServerAccess(env.KV, serverId);
    await touchRecentProject(env.KV, server, decodeURIComponent(project));
    await proxyAgent(server, `/api/projects/touch/${encodeURIComponent(project)}`, "POST");
    return json({ ok: true });
  }

  const readyMatch = path.match(/^\/api\/fleet\/([^/]+)\/ready\/([^/]+)$/);
  if (readyMatch && request.method === "GET") {
    const [, serverId, project] = readyMatch;
    const server = (await readServers(env.KV)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    const res = await proxyAgent(server, `/api/ready/${encodeURIComponent(project)}`);
    return json(await readJsonSafe(res), res.status);
  }

  return json({ error: "not found" }, 404);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname.startsWith("/api/")) {
      return handleApi(request, env, url);
    }

    if (url.pathname === "/version.json") {
      const cached = await env.KV.get(KV_UI_VERSION);
      if (cached) {
        return new Response(cached, {
          headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-cache" },
        });
      }
    }

    if (url.pathname === "/" || url.pathname === "/index.html") {
      const cached = await env.KV.get(KV_UI_HTML);
      if (cached) {
        return new Response(cached, {
          headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-cache" },
        });
      }
    }

    return env.ASSETS.fetch(request);
  },
};
