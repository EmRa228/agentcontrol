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
}

interface ServerRecord {
  servers: StoredServer[];
}

const KV_KEY = "servers";

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
    signal: AbortSignal.timeout(20000),
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
  return { id: s.id, name: s.name, url: s.url, addedAt: s.addedAt };
}

const STREAM_INTERVAL_MS = 4000;
const STREAM_HEARTBEAT_MS = 15000;

async function snapshotOneServer(server: StoredServer) {
  const base = { id: server.id, name: server.name, url: server.url };
  try {
    const [sysRes, foldRes] = await Promise.all([
      proxyAgent(server, "/api/system"),
      proxyAgent(server, "/api/folders"),
    ]);
    if (sysRes.status === 401 || foldRes.status === 401) {
      return { ...base, online: false, error: "auth failed" };
    }
    if (!sysRes.ok || !foldRes.ok) {
      return {
        ...base,
        online: false,
        error: `HTTP ${sysRes.status}/${foldRes.status}`,
      };
    }
    const system = (await sysRes.json()) as Record<string, unknown>;
    const foldersPayload = (await foldRes.json()) as { folders?: unknown[] };
    const folders = foldersPayload.folders || [];
    const cpu = system.cpu as { percent?: number; load_percent?: number; cores?: number } | undefined;
    const memory = system.memory as { percent?: number } | undefined;
    const disk = system.disk_root as { percent?: number } | undefined;
    const panel = system.panel as { running_workers?: number; project_count?: number } | undefined;
    return {
      ...base,
      online: true,
      summary: {
        cpu: cpu?.percent ?? null,
        load_pct: cpu?.load_percent ?? null,
        cores: cpu?.cores ?? null,
        ram: memory?.percent ?? null,
        disk: disk?.percent ?? null,
        workers: panel?.running_workers ?? 0,
        projects: panel?.project_count ?? folders.length,
      },
      system,
      folders,
    };
  } catch (e) {
    return { ...base, online: false, error: String(e) };
  }
}

async function buildFleetSnapshot(kv: KVNamespace) {
  const servers = await readServers(kv);
  const snapshots = await Promise.all(servers.map(snapshotOneServer));
  return { servers: snapshots, at: Date.now() };
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new Error("aborted"));
      return;
    }
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(new Error("aborted"));
      },
      { once: true },
    );
  });
}

function fleetStreamResponse(request: Request, env: Env): Response {
  const { readable, writable } = new TransformStream();
  const writer = writable.getWriter();
  const encoder = new TextEncoder();

  const pump = async () => {
    let lastHeartbeat = Date.now();
    try {
      while (!request.signal.aborted) {
        const payload = await buildFleetSnapshot(env.KV);
        await writer.write(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));

        const now = Date.now();
        if (now - lastHeartbeat >= STREAM_HEARTBEAT_MS) {
          await writer.write(encoder.encode(`: ping ${now}\n\n`));
          lastHeartbeat = now;
        }

        await sleep(STREAM_INTERVAL_MS, request.signal);
      }
    } catch {
      // client disconnected or sleep aborted
    } finally {
      try {
        await writer.close();
      } catch {
        /* already closed */
      }
    }
  };

  void pump();

  return new Response(readable, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}

async function handleApi(request: Request, env: Env, url: URL): Promise<Response> {
  const path = url.pathname;

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

  if (path === "/api/fleet/stream" && request.method === "GET") {
    return fleetStreamResponse(request, env);
  }

  if (path === "/api/fleet/snapshot" && request.method === "GET") {
    return json(await buildFleetSnapshot(env.KV));
  }

  const startMatch = path.match(/^\/api\/fleet\/([^/]+)\/start\/([^/]+)$/);
  if (startMatch && request.method === "POST") {
    const [, serverId, project] = startMatch;
    const server = (await readServers(env.KV)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    const res = await proxyAgent(server, `/api/start/${encodeURIComponent(project)}`, "POST");
    return json(await readJsonSafe(res), res.status);
  }

  const stopMatch = path.match(/^\/api\/fleet\/([^/]+)\/stop\/([^/]+)$/);
  if (stopMatch && request.method === "POST") {
    const [, serverId, project] = stopMatch;
    const server = (await readServers(env.KV)).find((s) => s.id === serverId);
    if (!server) return json({ error: "server not found" }, 404);
    const res = await proxyAgent(server, `/api/stop/${encodeURIComponent(project)}`, "POST");
    return json(await readJsonSafe(res), res.status);
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

    return env.ASSETS.fetch(request);
  },
};
