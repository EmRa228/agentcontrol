import fleetVersion from "../version.json";
import {
  KV_NOTIFY,
  defaultNotifyConfig,
  notifyFleet,
  publicNotifyConfig,
  readNotifyConfig,
  type FleetNotifyConfig,
} from "./notify";
import { json } from "./http";
import {
  createFleetSession,
  fleetPasswordConfigured,
  protectSecret,
  requireFleetAuth,
  revealSecret,
  revokeFleetSession,
  sameOriginRequest,
  sessionCookie,
  verifyFleetPassword,
  writeFleetPassword,
} from "./auth";

export interface Env {
  KV: KVNamespace;
  ASSETS: Fetcher;
  FLEET_PASSWORD: string;
  /** Optional base64 32-byte key: encrypts stored server passwords (AES-GCM). */
  FLEET_ENC_KEY?: string;
}

interface StoredServer {
  id: string;
  name: string;
  url: string;
  password: string;
  addedAt: number;
  lastAccessedAt?: number;
  paused?: boolean;
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
  agent_url?: string;
}



const KV_KEY = "servers";
const KV_UI_HTML = "fleet_ui_html";
const KV_UI_VERSION = "fleet_ui_version";
const KV_RECENT = "recent_projects";
const KV_SNAPSHOT = "fleet_snapshot_cache";
const KV_HEALTH_STATE = "fleet_health_state";
const KV_HEALTH_PREFIX = "fleet_health:";
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
      const kvVersion = JSON.parse(cached) as VersionInfo;
      if (versionIsNewer(FLEET_VERSION.version, kvVersion.version)) {
        return FLEET_VERSION;
      }
      return kvVersion;
    } catch {
      /* fall through */
    }
  }
  return FLEET_VERSION;
}

async function kvHtmlIsCurrent(kv: KVNamespace): Promise<boolean> {
  const html = await kv.get(KV_UI_HTML);
  if (!html) return false;
  const cached = await kv.get(KV_UI_VERSION);
  if (!cached) return false;
  try {
    const kvVersion = JSON.parse(cached) as VersionInfo;
    return !versionIsNewer(FLEET_VERSION.version, kvVersion.version);
  } catch {
    return false;
  }
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
    "ac-tg-uk.aksbaz.com → 193.163.201.14, then use https://ac-tg-uk.aksbaz.com:30228"
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

const loginAttempts = new Map<string, { count: number; first: number }>();

async function readServers(kv: KVNamespace, env?: Env): Promise<StoredServer[]> {
  const raw = await kv.get(KV_KEY);
  if (!raw) return [];
  try {
    const data = JSON.parse(raw) as ServerRecord;
    const servers = data.servers || [];
    if (env?.FLEET_ENC_KEY) {
      for (const server of servers) {
        try {
          server.password = await revealSecret(server.password, env);
        } catch {
          server.password = "";
        }
      }
    }
    return servers;
  } catch {
    return [];
  }
}

async function writeServers(kv: KVNamespace, servers: StoredServer[], env?: Env): Promise<void> {
  let stored = servers;
  if (env?.FLEET_ENC_KEY) {
    stored = await Promise.all(
      servers.map(async (server) => ({ ...server, password: await protectSecret(server.password, env) })),
    );
  }
  const payload: ServerRecord = { servers: stored };
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
  await updateRecentProject(kv, server, project, { bumpActivity: true, running });
}

async function updateRecentProject(
  kv: KVNamespace,
  server: StoredServer,
  project: string,
  opts: { bumpActivity?: boolean; running?: boolean } = {},
): Promise<void> {
  const items = await readRecentProjects(kv);
  const now = Date.now();
  const existing = items.find((i) => i.serverId === server.id && i.project === project);
  const filtered = items.filter((i) => !(i.serverId === server.id && i.project === project));
  filtered.unshift({
    serverId: server.id,
    serverName: server.name,
    serverUrl: server.url,
    project,
    touchedAt: opts.bumpActivity ? now : (existing?.touchedAt ?? 0),
    running: opts.running ?? existing?.running,
    agent_url: existing?.agent_url,
  });
  await writeRecentProjects(kv, filtered);
}

async function touchServerAccess(kv: KVNamespace, serverId: string, env?: Env): Promise<void> {
  const servers = await readServers(kv, env);
  const idx = servers.findIndex((s) => s.id === serverId);
  if (idx < 0) return;
  servers[idx].lastAccessedAt = Date.now();
  await writeServers(kv, servers, env);
}

async function proxyAgent(
  server: StoredServer,
  path: string,
  method = "GET",
  body?: unknown,
): Promise<Response> {
  const base = server.url.replace(/\/$/, "");
  const authHeaders: Record<string, string> = server.password.startsWith("ac_")
    ? { "X-AgentControl-Token": server.password }
    : { "X-AgentControl-Auth": server.password };
  const init: RequestInit = {
    method,
    headers: {
      ...authHeaders,
      Accept: "application/json",
    },
    // Handle the panel's HTTP -> HTTPS redirect ourselves so the auth header
    // survives the scheme change.
    redirect: "manual",
    signal: AbortSignal.timeout(45000),
  };
  if (body !== undefined) {
    init.headers = { ...init.headers, "content-type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const httpsBase = base.startsWith("http://") ? `https://${base.slice("http://".length)}` : "";
  const send = (origin: string) => fetch(`${origin}${path}`, init);

  let res: Response;
  try {
    res = await send(base);
  } catch (e) {
    // A stored http:// URL may now be a TLS-only panel port. Retry over https
    // so certificates can be enabled without editing every Fleet entry first.
    if (!httpsBase) throw e;
    return send(httpsBase);
  }
  // Caddy answers plaintext HTTP sent to its TLS port with 400, and the panel's
  // same-port front answers with a 308 redirect. Retry the https origin directly
  // (custom auth headers are dropped when fetch follows a cross-origin redirect).
  if (httpsBase && (res.status === 400 || [301, 302, 307, 308].includes(res.status))) {
    try {
      return await send(httpsBase);
    } catch {
      return res;
    }
  }
  return res;
}

async function readJsonSafe(res: Response): Promise<unknown> {
  const text = await res.text();
  if (text.includes("error code: 1003")) {
    return {
      error: "Cloudflare error 1003 — use a hostname, not an IP address",
      status: res.status,
      hint: "Add an A record (DNS only) in your zone and use https://name.aksbaz.com:30228",
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
    paused: Boolean(s.paused),
  };
}

function findDuplicateServer(servers: StoredServer[], name: string, baseUrl: string): StoredServer | null {
  const urlKey = baseUrl.toLowerCase();
  const nameKey = name.toLowerCase();
  for (const s of servers) {
    try {
      if (normalizeUrl(s.url).toLowerCase() === urlKey) return s;
    } catch {
      /* skip invalid stored url */
    }
    if (s.name.toLowerCase() === nameKey) return s;
  }
  return null;
}

function pausedServerSnapshot(server: StoredServer) {
  return {
    id: server.id,
    name: server.name,
    url: server.url,
    lastAccessedAt: server.lastAccessedAt || server.addedAt,
    online: false,
    paused: true,
    error: "paused by user",
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

function activityTs(ts: number | undefined | null): number {
  if (!ts) return 0;
  return ts < 1e12 ? ts * 1000 : ts;
}

function folderAgentUrl(folder: { agent_url?: string; worker_id?: string }): string | undefined {
  if (folder.agent_url) return folder.agent_url;
  const id = folder.worker_id;
  if (!id) return undefined;
  return `https://cursor.com/agents#workerId=${id}`;
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
      agent_url?: string;
      worker_id?: string;
    }>;
    for (const f of folders) {
      fromSnapshots.push({
        serverId: String(s.id),
        serverName: String(s.name),
        serverUrl: String(s.url),
        project: f.name,
        touchedAt: activityTs(f.touched_at || f.mtime),
        running: f.running,
        agent_url: folderAgentUrl(f),
      });
    }
  }
  const merged = new Map<string, RecentProject>();
  for (const item of [...kvRecent, ...fromSnapshots]) {
    const key = `${item.serverId}:${item.project}`;
    const existing = merged.get(key);
    const itemTs = activityTs(item.touchedAt);
    const existingTs = existing ? activityTs(existing.touchedAt) : 0;
    if (!existing || itemTs > existingTs) {
      merged.set(key, { ...existing, ...item, touchedAt: Math.max(itemTs, existingTs) });
    } else if (item.agent_url || item.running !== undefined) {
      merged.set(key, {
        ...item,
        ...existing,
        touchedAt: existingTs,
        agent_url: item.agent_url || existing.agent_url,
        running: item.running ?? existing.running,
      });
    }
  }
  return [...merged.values()].sort((a, b) => activityTs(b.touchedAt) - activityTs(a.touchedAt)).slice(0, limit);
}

async function snapshotOneServer(server: StoredServer) {
  const base = {
    id: server.id,
    name: server.name,
    url: server.url,
    lastAccessedAt: server.lastAccessedAt || server.addedAt,
    paused: Boolean(server.paused),
  };
  if (server.paused) {
    return pausedServerSnapshot(server);
  }
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
    const raw = await bundleRes.text();
    let bundle: {
      system?: Record<string, unknown>;
      folders?: unknown[];
      history?: unknown[];
      version?: VersionInfo;
      proxy?: Record<string, unknown>;
      health?: Record<string, unknown>;
      agent?: Record<string, unknown>;
      opencode?: Record<string, unknown>;
    };
    try {
      bundle = JSON.parse(raw) as typeof bundle;
    } catch {
      return { ...base, online: false, error: "invalid JSON (response truncated?)" };
    }
    const system = bundle.system || {};
    const folders = (bundle.folders || []) as Array<Record<string, unknown>>;
    const cpu = system.cpu as { percent?: number; load_percent?: number; cores?: number } | undefined;
    const memory = system.memory as {
      percent?: number;
      swap_percent?: number | null;
      swap_used_human?: string | null;
    } | undefined;
    const disk = system.disk_root as {
      percent?: number;
      used_human?: string;
      total_human?: string;
    } | undefined;
    const diskIo = system.disk_io as { busy_percent?: number | null } | undefined;
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
      health: bundle.health || null,
      agent: bundle.agent || null,
      opencode: bundle.opencode || null,
      summary: {
        cpu: cpu?.percent ?? null,
        load_pct: cpu?.load_percent ?? null,
        cores: cpu?.cores ?? null,
        ram: memory?.percent ?? null,
        swap: memory?.swap_percent ?? null,
        disk: disk?.percent ?? null,
        disk_used: disk?.used_human ?? null,
        disk_total: disk?.total_human ?? null,
        disk_io: diskIo?.busy_percent ?? null,
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

async function buildFleetSnapshot(kv: KVNamespace, env?: Env) {
  const servers = await readServers(kv, env);
  const snapshots = await Promise.all(servers.map(snapshotOneServer));
  const sorted = sortSnapshotsByRecent(snapshots);
  const recentKv = await readRecentProjects(kv);
  const recentProjects = aggregateRecentProjects(sorted, recentKv, 10);
  const overview = buildOverview(sorted);
  const payload = {
    servers: sorted,
    recentProjects,
    recentProjectsAll: aggregateRecentProjects(sorted, recentKv, 500),
    overview,
    at: Date.now(),
  };
  await kv.put(KV_SNAPSHOT, JSON.stringify(payload));
  return payload;
}

async function readCachedSnapshot(kv: KVNamespace): Promise<Record<string, unknown> | null> {
  const raw = await kv.get(KV_SNAPSHOT);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return null;
  }
}

async function reconcileSnapshotWithRegistry(kv: KVNamespace, cached: Record<string, unknown> | null, env?: Env) {
  const registered = await readServers(kv, env);
  if (!cached) return buildFleetSnapshot(kv, env);

  const registeredIds = new Set(registered.map((s) => s.id));
  const serverList = ((cached.servers as Array<Record<string, unknown>>) || []).filter((s) =>
    registeredIds.has(String(s.id)),
  );
  const cachedIds = new Set(serverList.map((s) => String(s.id)));

  for (const s of registered) {
    if (!cachedIds.has(s.id)) {
      serverList.push(
        s.paused
          ? pausedServerSnapshot(s)
          : {
              id: s.id,
              name: s.name,
              url: s.url,
              lastAccessedAt: s.lastAccessedAt || s.addedAt,
              online: false,
              paused: false,
              error: "pending refresh",
            },
      );
    }
  }

  for (const entry of serverList) {
    const reg = registered.find((s) => s.id === String(entry.id));
    if (reg) entry.paused = Boolean(reg.paused);
  }

  const sorted = sortSnapshotsByRecent(serverList as Array<{ id: string; lastAccessedAt?: number }>);
  const recentKv = await readRecentProjects(kv);
  return {
    servers: sorted,
    recentProjects: aggregateRecentProjects(sorted, recentKv, 10),
    recentProjectsAll: aggregateRecentProjects(sorted, recentKv, 500),
    overview: buildOverview(sorted),
    at: (cached.at as number) || Date.now(),
  };
}

async function buildFleetSnapshotFromCache(kv: KVNamespace, env?: Env) {
  const cached = await readCachedSnapshot(kv);
  return reconcileSnapshotWithRegistry(kv, cached, env);
}

async function removeServerFromSnapshot(kv: KVNamespace, serverId: string) {
  const cached = await readCachedSnapshot(kv);
  if (!cached) return;
  const serverList = ((cached.servers as Array<Record<string, unknown>>) || []).filter(
    (s) => String(s.id) !== serverId,
  );
  const recentKv = await readRecentProjects(kv);
  const sorted = sortSnapshotsByRecent(serverList as Array<{ id: string; lastAccessedAt?: number }>);
  const payload = {
    servers: sorted,
    recentProjects: aggregateRecentProjects(sorted, recentKv, 10),
    recentProjectsAll: aggregateRecentProjects(sorted, recentKv, 500),
    overview: buildOverview(sorted),
    at: Date.now(),
  };
  await kv.put(KV_SNAPSHOT, JSON.stringify(payload));
}


async function refreshFleetServer(kv: KVNamespace, serverId: string, persist = false, env?: Env) {
  const servers = await readServers(kv, env);
  const server = servers.find((s) => s.id === serverId);
  if (!server) return { error: "server not found" };

  const snapshot = server.paused ? pausedServerSnapshot(server) : await snapshotOneServer(server);
  const cached = (await readCachedSnapshot(kv)) || {
    servers: [],
    recentProjects: [],
    recentProjectsAll: [],
    overview: { ok: true, online: 0, offline: 0, workers: 0, proxyIssues: 0, alerts: [] },
    at: 0,
  };
  const serverList = (cached.servers as Array<Record<string, unknown>>) || [];
  const idx = serverList.findIndex((s) => s.id === serverId);
  if (idx >= 0) serverList[idx] = snapshot;
  else serverList.push(snapshot);

  const sorted = sortSnapshotsByRecent(serverList as Array<{ id: string; lastAccessedAt?: number }>);
  const recentKv = await readRecentProjects(kv);
  const payload = {
    servers: sorted,
    recentProjects: aggregateRecentProjects(sorted, recentKv, 10),
    recentProjectsAll: aggregateRecentProjects(sorted, recentKv, 500),
    overview: buildOverview(sorted),
    at: Date.now(),
  };
  if (persist) {
    await kv.put(KV_SNAPSHOT, JSON.stringify(payload));
  }
  return { server: snapshot, snapshot: payload };
}

async function pushProxyToServers(kv: KVNamespace, config: Record<string, unknown>, env?: Env) {
  const servers = await readServers(kv, env);
  const results = [];
  for (const server of servers.filter((s) => !s.paused)) {
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

async function updateAllPanels(kv: KVNamespace, env?: Env) {
  const servers = (await readServers(kv, env)).filter((s) => !s.paused);
  const results: Array<Record<string, unknown>> = [];
  if (!servers.length) return results;

  const updateOne = async (server: StoredServer) => {
    try {
      const res = await proxyAgent(server, "/api/update/apply", "POST");
      return {
        server: server.name,
        id: server.id,
        ok: res.ok || res.status === 202,
        status: res.status,
        body: await readJsonSafe(res),
      };
    } catch (e) {
      return { server: server.name, id: server.id, ok: false, error: String(e) };
    }
  };

  const canary = servers[0];
  let before = "";
  try {
    const res = await proxyAgent(canary, "/health");
    if (res.ok) before = String(((await res.json()) as { version?: string }).version || "");
  } catch {
    /* canary may be offline; update result will report it */
  }
  const first: Record<string, unknown> = { ...(await updateOne(canary)), canary: true };
  results.push(first);
  if (!(first.ok as boolean)) {
    results.push({ skipped: true, reason: "canary update failed — remaining servers untouched" });
    return results;
  }
  // Wait for the canary to come back healthy (up to 2 minutes).
  let healthy = false;
  const deadline = Date.now() + 120_000;
  const startedAt = Date.now();
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 5000));
    try {
      const res = await proxyAgent(canary, "/health");
      if (res.ok) {
        const version = String(((await res.json()) as { version?: string }).version || "");
        // A no-op update restarts the panel without changing the version; accept
        // it once the panel has stayed healthy for a grace period.
        if (!before || version !== before || Date.now() - startedAt > 45_000) {
          healthy = true;
          break;
        }
      }
    } catch {
      /* keep waiting */
    }
  }
  if (!healthy) {
    results.push({ canary: true, ok: false, error: "canary did not become healthy; rollout stopped" });
    return results;
  }
  for (const server of servers.slice(1)) {
    results.push(await updateOne(server));
  }
  return results;
}

function serverUrlFor(server: StoredServer): string {
  return server.url.replace(/\/$/, "");
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
    if (!sameOriginRequest(request)) return json({ error: "cross-origin request rejected" }, 403);
    const ip = request.headers.get("CF-Connecting-IP") || "unknown";
    if (await env.KV.get(`fleet_lock:${ip}`)) {
      return json({ error: "too many failed attempts — try again in 5 minutes", retry_after: 300 }, 429);
    }
    const data = (await request.json().catch(() => ({}))) as { password?: string };
    if (!(await fleetPasswordConfigured(env.KV, env))) {
      return json({ error: "setup required", needs_password: true }, 403);
    }
    const password = data.password || "";
    const result = await verifyFleetPassword(env.KV, env, password);
    if (!result.ok) {
      const now = Date.now();
      const entry = loginAttempts.get(ip) || { count: 0, first: now };
      if (now - entry.first > 300_000) {
        entry.count = 0;
        entry.first = now;
      }
      entry.count += 1;
      loginAttempts.set(ip, entry);
      if (entry.count >= 10) {
        loginAttempts.delete(ip);
        await env.KV.put(`fleet_lock:${ip}`, "1", { expirationTtl: 300 });
        return json({ error: "too many failed attempts — try again in 5 minutes", retry_after: 300 }, 429);
      }
      return json({ error: "wrong password", attempts_left: 10 - entry.count }, 401);
    }
    loginAttempts.delete(ip);
    if (result.legacy) await writeFleetPassword(env.KV, password);
    const token = await createFleetSession(request, env);
    const response = json({ ok: true });
    response.headers.append("Set-Cookie", sessionCookie(request, token));
    return response;
  }

  if (path === "/api/logout" && request.method === "POST") {
    await revokeFleetSession(request, env);
    const response = json({ ok: true });
    response.headers.append("Set-Cookie", sessionCookie(request, "", 0));
    return response;
  }

  if (path === "/api/setup/status" && request.method === "GET") {
    const configured = await fleetPasswordConfigured(env.KV, env);
    return json({ needs_password: !configured });
  }

  if (path === "/api/setup" && request.method === "POST") {
    if (!sameOriginRequest(request)) return json({ error: "cross-origin request rejected" }, 403);
    if (await fleetPasswordConfigured(env.KV, env)) {
      return json({ error: "already configured" }, 403);
    }
    const data = (await request.json().catch(() => ({}))) as { password?: string };
    const password = (data.password || "").trim();
    if (password.length < 8) {
      return json({ error: "password must be at least 8 characters" }, 400);
    }
    await writeFleetPassword(env.KV, password);
    const token = await createFleetSession(request, env);
    const response = json({ ok: true });
    response.headers.append("Set-Cookie", sessionCookie(request, token));
    return response;
  }

  const authErr = await requireFleetAuth(request, env);
  if (authErr) return authErr;

  if (path === "/api/update/apply" && request.method === "POST") {
    try {
      const version = await applyFleetUiUpdate(env.KV);
      return json({ status: "updated", version });
    } catch (e) {
      return json({ error: String(e) }, 500);
    }
  }

  if (path === "/api/notifications" && request.method === "GET") {
    return json({ config: publicNotifyConfig(await readNotifyConfig(env.KV)) });
  }

  if (path === "/api/notifications" && request.method === "POST") {
    const current = await readNotifyConfig(env.KV);
    const data = (await request.json().catch(() => ({}))) as Partial<FleetNotifyConfig>;
    const next: FleetNotifyConfig = {
      ...current,
      ...data,
      events: { ...current.events, ...(data.events || {}) },
      channels: {
        webhook: { ...current.channels.webhook, ...(data.channels?.webhook || {}) },
        telegram: { ...current.channels.telegram, ...(data.channels?.telegram || {}) },
      },
    };
    if (!next.channels.webhook.url && next.channels.webhook.enabled) {
      return json({ error: "webhook url required" }, 400);
    }
    if (!next.channels.webhook.secret) next.channels.webhook.secret = current.channels.webhook.secret;
    if (!next.channels.telegram.bot_token) next.channels.telegram.bot_token = current.channels.telegram.bot_token;
    await env.KV.put(KV_NOTIFY, JSON.stringify(next));
    return json({ ok: true, config: publicNotifyConfig(next) });
  }

  if (path === "/api/notifications/test" && request.method === "POST") {
    const config = await readNotifyConfig(env.KV);
    await notifyFleet(config, "info", "AgentControl Fleet test", `Test notification from ${new URL(request.url).host}`);
    return json({ ok: true });
  }

  const healthHistoryMatch = path.match(/^\/api\/fleet\/health\/([^/]+)$/);
  if (healthHistoryMatch && request.method === "GET") {
    const raw = await env.KV.get(KV_HEALTH_PREFIX + healthHistoryMatch[1]);
    return json({ points: raw ? JSON.parse(raw) : [] });
  }

  if (path === "/api/servers" && request.method === "GET") {
    const servers = await readServers(env.KV, env);
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

    const servers = await readServers(env.KV, env);
    const duplicate = findDuplicateServer(servers, name, baseUrl);
    if (duplicate) {
      return json(
        {
          error: "A server with this name or URL is already registered",
          server: publicServer(duplicate),
        },
        409,
      );
    }

    const entry: StoredServer = {
      id: crypto.randomUUID(),
      name,
      url: baseUrl,
      password,
      addedAt: Date.now(),
      lastAccessedAt: Date.now(),
    };
    servers.push(entry);
    await writeServers(env.KV, servers, env);
    const refreshed = await refreshFleetServer(env.KV, entry.id, true, env);
    if ("error" in refreshed) {
      return json({ server: publicServer(entry), warning: "saved but snapshot refresh failed" });
    }
    return json({ server: publicServer(entry), snapshot: refreshed.snapshot });
  }

  const deleteMatch = path.match(/^\/api\/servers\/([^/]+)$/);
  if (deleteMatch && request.method === "DELETE") {
    const id = deleteMatch[1];
    const servers = (await readServers(env.KV, env)).filter((s) => s.id !== id);
    await writeServers(env.KV, servers, env);
    await removeServerFromSnapshot(env.KV, id);
    return json({ ok: true });
  }

  const pauseMatch = path.match(/^\/api\/servers\/([^/]+)\/pause$/);
  if (pauseMatch && request.method === "POST") {
    const id = pauseMatch[1];
    const data = (await request.json().catch(() => ({}))) as { paused?: boolean };
    const servers = await readServers(env.KV, env);
    const idx = servers.findIndex((s) => s.id === id);
    if (idx < 0) return json({ error: "server not found" }, 404);
    const nextPaused = data.paused !== undefined ? Boolean(data.paused) : !servers[idx].paused;
    servers[idx].paused = nextPaused;
    await writeServers(env.KV, servers, env);
    const refreshed = await refreshFleetServer(env.KV, id, true, env);
    if ("error" in refreshed) {
      return json({ ok: true, server: publicServer(servers[idx]) });
    }
    return json({ ok: true, server: publicServer(servers[idx]), snapshot: refreshed.snapshot });
  }

  if (path === "/api/servers/touch" && request.method === "POST") {
    const data = (await request.json().catch(() => ({}))) as { serverId?: string };
    const serverId = String(data.serverId || "").trim();
    if (!serverId) return json({ error: "serverId required" }, 400);
    await touchServerAccess(env.KV, serverId, env);
    return json({ ok: true });
  }

  if (path === "/api/fleet/snapshot" && request.method === "GET") {
    return json(await buildFleetSnapshot(env.KV, env));
  }

  if (path === "/api/fleet/cache" && request.method === "GET") {
    const cached = await buildFleetSnapshotFromCache(env.KV, env);
    return json({ ...cached, cached: true });
  }

  const serverRefreshMatch = path.match(/^\/api\/fleet\/server\/([^/]+)$/);
  if (serverRefreshMatch && request.method === "GET") {
    const serverId = serverRefreshMatch[1];
    const persist = url.searchParams.get("persist") === "1";
    const result = await refreshFleetServer(env.KV, serverId, persist, env);
    if ("error" in result) return json(result, 404);
    return json(result);
  }

  if (path === "/api/fleet/update-all" && request.method === "POST") {
    const results = await updateAllPanels(env.KV, env);
    return json({ ok: true, results });
  }

  if (path === "/api/fleet/proxy/push" && request.method === "POST") {
    const data = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const results = await pushProxyToServers(env.KV, data, env);
    return json({ ok: results.every((r) => r.ok), results });
  }

  const startMatch = path.match(/^\/api\/fleet\/([^/]+)\/start\/([^/]+)$/);
  if (startMatch && request.method === "POST") {
    const [, serverId, project] = startMatch;
    const server = (await readServers(env.KV, env)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    await touchServerAccess(env.KV, serverId, env);
    await touchRecentProject(env.KV, server, decodeURIComponent(project), true);
    const res = await proxyAgent(server, `/api/start/${encodeURIComponent(project)}`, "POST");
    return json(await readJsonSafe(res), res.status);
  }

  const restartMatch = path.match(/^\/api\/fleet\/([^/]+)\/restart\/([^/]+)$/);
  if (restartMatch && request.method === "POST") {
    const [, serverId, project] = restartMatch;
    const server = (await readServers(env.KV, env)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    await touchServerAccess(env.KV, serverId, env);
    await touchRecentProject(env.KV, server, decodeURIComponent(project), true);
    const res = await proxyAgent(server, `/api/restart/${encodeURIComponent(project)}`, "POST");
    return json(await readJsonSafe(res), res.status);
  }

  const stopMatch = path.match(/^\/api\/fleet\/([^/]+)\/stop\/([^/]+)$/);
  if (stopMatch && request.method === "POST") {
    const [, serverId, project] = stopMatch;
    const server = (await readServers(env.KV, env)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    await touchServerAccess(env.KV, serverId, env);
    await updateRecentProject(env.KV, server, decodeURIComponent(project), { running: false });
    const res = await proxyAgent(server, `/api/stop/${encodeURIComponent(project)}`, "POST");
    return json(await readJsonSafe(res), res.status);
  }

  const touchMatch = path.match(/^\/api\/fleet\/([^/]+)\/touch\/([^/]+)$/);
  if (touchMatch && request.method === "POST") {
    const [, serverId, project] = touchMatch;
    const server = (await readServers(env.KV, env)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    await touchServerAccess(env.KV, serverId, env);
    await touchRecentProject(env.KV, server, decodeURIComponent(project));
    await proxyAgent(server, `/api/projects/touch/${encodeURIComponent(project)}`, "POST");
    return json({ ok: true });
  }

  const readyMatch = path.match(/^\/api\/fleet\/([^/]+)\/ready\/([^/]+)$/);
  if (readyMatch && request.method === "GET") {
    const [, serverId, project] = readyMatch;
    const server = (await readServers(env.KV, env)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    const res = await proxyAgent(server, `/api/ready/${encodeURIComponent(project)}`);
    return json(await readJsonSafe(res), res.status);
  }

  const opencodeInstallMatch = path.match(/^\/api\/fleet\/([^/]+)\/opencode-install$/);
  if (opencodeInstallMatch && (request.method === "GET" || request.method === "POST")) {
    const [, serverId] = opencodeInstallMatch;
    const server = (await readServers(env.KV, env)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    const res = await proxyAgent(server, "/api/opencode/install", request.method);
    return json(await readJsonSafe(res), res.status);
  }

  const opencodeMatch = path.match(/^\/api\/fleet\/([^/]+)\/opencode\/([^/]+)$/);
  if (opencodeMatch && request.method === "POST") {
    const [, serverId, projectSegment] = opencodeMatch;
    const project = decodeURIComponent(projectSegment);
    const server = (await readServers(env.KV, env)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    await touchServerAccess(env.KV, serverId, env);
    await touchRecentProject(env.KV, server, project, true);
    const res = await proxyAgent(
      server,
      `/api/opencode/open/${encodeURIComponent(project)}`,
      "POST",
    );
    const body = await readJsonSafe(res);
    if (body && typeof body === "object") {
      const publicBase =
        "public_base" in body && typeof body.public_base === "string" && body.public_base
          ? body.public_base.replace(/\/$/, "")
          : server.url.replace(/\/$/, "");
      const panelUrl = `${publicBase}/?${new URLSearchParams({ opencode: project })}`;
      if (res.ok && "opencode_launch_path" in body && typeof body.opencode_launch_path === "string") {
        return json(
          {
            ...body,
            fleet_opencode_url: `${publicBase}${body.opencode_launch_path}`,
            fleet_panel_url: panelUrl,
          },
          res.status,
        );
      }
      if (res.ok && "opencode_path" in body && typeof body.opencode_path === "string") {
        return json(
          {
            ...body,
            fleet_opencode_url: `${publicBase}${body.opencode_path}`,
            fleet_panel_url: panelUrl,
          },
          res.status,
        );
      }
      if (res.status === 409 && "status" in body && body.status === "install_required") {
        return json({ ...body, fleet_panel_url: panelUrl }, res.status);
      }
    }
    return json(body, res.status);
  }

  return json({ error: "not found" }, 404);
}

async function scheduledHealthCheck(env: Env): Promise<void> {
  const config = await readNotifyConfig(env.KV);
  const servers = (await readServers(env.KV, env)).filter((server) => !server.paused);
  await Promise.all(
    servers.map(async (server) => {
      const stateKey = `${KV_HEALTH_STATE}:${server.id}`;
      let state: {
        status?: string;
        consecutive?: number;
        notified?: boolean;
        lastCheck?: number;
        lastSample?: number;
      } = {};
      try {
        const raw = await env.KV.get(stateKey);
        state = raw ? JSON.parse(raw) : {};
      } catch {
        state = {};
      }
      let ok = false;
      let health: Record<string, unknown> = {};
      let error = "";
      try {
        const res = await proxyAgent(server, "/api/health");
        ok = res.ok;
        if (ok) health = (await res.json()) as Record<string, unknown>;
        else error = `HTTP ${res.status}`;
      } catch (e) {
        error = String(e);
      }
      const now = Date.now();
      if (ok) {
        if (state.status === "down" && state.notified && config.events.recovered) {
          await notifyFleet(
            config,
            "info",
            `${server.name} recovered`,
            `${server.name} (${serverUrlFor(server)}) is responding again.`,
          );
        }
        const shouldWrite = state.status !== "ok" || Boolean(state.consecutive) || !state.lastCheck;
        const shouldSample = !state.lastSample || now - state.lastSample > 3_600_000;
        state = { ...state, status: "ok", consecutive: 0, notified: false, lastCheck: now };
        if (shouldSample) {
          state.lastSample = now;
          try {
            const rawHistory = await env.KV.get(KV_HEALTH_PREFIX + server.id);
            const history = rawHistory ? (JSON.parse(rawHistory) as Array<Record<string, unknown>>) : [];
            history.push({
              t: now,
              status: health.status || "ok",
              uptime_seconds: health.uptime_seconds ?? null,
              workers_running: health.workers_running ?? null,
              disk: (health.disk as { percent?: number } | undefined)?.percent ?? null,
              cert_days: (health.certificate as { days_left?: number } | undefined)?.days_left ?? null,
            });
            await env.KV.put(KV_HEALTH_PREFIX + server.id, JSON.stringify(history.slice(-168)));
          } catch {
            /* history is best-effort */
          }
        }
        if (shouldWrite || shouldSample) await env.KV.put(stateKey, JSON.stringify(state));
      } else {
        const consecutive = (state.consecutive || 0) + 1;
        const down = consecutive >= Math.max(1, config.fail_threshold || 2);
        const alreadyDown = state.status === "down";
        if (down && !state.notified && config.events.down) {
          await notifyFleet(
            config,
            "critical",
            `${server.name} is down`,
            `${server.name} (${serverUrlFor(server)}) failed ${consecutive} checks: ${error || "unreachable"}`,
          );
        }
        // Once a server is marked down, stop writing on every cron tick: one
        // failure write is enough until it recovers or crosses the threshold.
        if (!alreadyDown) {
          await env.KV.put(
            stateKey,
            JSON.stringify({
              ...state,
              status: down ? "down" : "unknown",
              consecutive,
              notified: down ? true : Boolean(state.notified),
              lastCheck: now,
            }),
          );
        }
      }
    }),
  );
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname.startsWith("/api/")) {
      return handleApi(request, env, url);
    }

    if (url.pathname === "/version.json") {
      const version = await getActiveFleetVersion(env.KV);
      return new Response(JSON.stringify(version), {
        headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-cache" },
      });
    }

    if (url.pathname === "/" || url.pathname === "/index.html") {
      if (!(await kvHtmlIsCurrent(env.KV))) {
        try {
          await applyFleetUiUpdate(env.KV);
        } catch {
          /* serve bundled assets below */
        }
      }
      if (await kvHtmlIsCurrent(env.KV)) {
        const cached = await env.KV.get(KV_UI_HTML);
        if (cached) {
          return new Response(cached, {
            headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-cache" },
          });
        }
      }
    }

    return env.ASSETS.fetch(request);
  },

  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(scheduledHealthCheck(env));
  },
};
