import type { Env } from "./index";
import { json } from "./http";

export const KV_AUTH = "fleet_auth";
export const KV_PASSWORD = "fleet_password";
export const KV_SESSION_PREFIX = "fleet_session:";
export const SESSION_TTL_SECONDS = 30 * 24 * 60 * 60;

export interface AuthRecord {
  v: 1;
  salt: string;
  hash: string;
}

export function b64(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let binary = "";
  for (const byte of view) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export function unb64(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export async function pbkdf2(password: string, salt: Uint8Array): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations: 25000, hash: "SHA-256" },
    key,
    256,
  );
  return b64(bits);
}

export async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function readAuth(kv: KVNamespace): Promise<AuthRecord | null> {
  const raw = await kv.get(KV_AUTH);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as AuthRecord;
    return parsed.salt && parsed.hash ? parsed : null;
  } catch {
    return null;
  }
}

export async function fleetPasswordConfigured(kv: KVNamespace, env: Env): Promise<boolean> {
  if (await readAuth(kv)) return true;
  if (await kv.get(KV_PASSWORD)) return true;
  return Boolean((env.FLEET_PASSWORD || "").trim());
}

export async function verifyFleetPassword(kv: KVNamespace, env: Env, password: string): Promise<{ ok: boolean; legacy: boolean }> {
  if (!password) return { ok: false, legacy: false };
  const record = await readAuth(kv);
  if (record) {
    const candidate = await pbkdf2(password, unb64(record.salt));
    return { ok: candidate === record.hash, legacy: false };
  }
  const legacy = ((await kv.get(KV_PASSWORD)) || env.FLEET_PASSWORD || "").trim();
  if (!legacy) return { ok: false, legacy: false };
  return { ok: legacy === password, legacy: true };
}

export async function writeFleetPassword(kv: KVNamespace, password: string): Promise<void> {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const hash = await pbkdf2(password, salt);
  const record: AuthRecord = { v: 1, salt: b64(salt), hash };
  await kv.put(KV_AUTH, JSON.stringify(record));
  await kv.delete(KV_PASSWORD);
}

export function requestCookie(request: Request, name: string): string {
  const header = request.headers.get("Cookie") || "";
  for (const part of header.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return rest.join("=");
  }
  return "";
}

export function sessionCookie(request: Request, token: string, maxAge = SESSION_TTL_SECONDS): string {
  const secure = new URL(request.url).protocol === "https:" ? "; Secure" : "";
  return `fleet_session=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${secure}`;
}

export async function createFleetSession(request: Request, env: Env): Promise<string> {
  const token = [...crypto.getRandomValues(new Uint8Array(32))]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  await env.KV.put(
    KV_SESSION_PREFIX + (await sha256Hex(token)),
    JSON.stringify({ created: Date.now(), ua: request.headers.get("User-Agent") || "" }),
    { expirationTtl: SESSION_TTL_SECONDS },
  );
  return token;
}

export async function fleetSessionValid(request: Request, env: Env): Promise<boolean> {
  const token = requestCookie(request, "fleet_session");
  if (!token) return false;
  return Boolean(await env.KV.get(KV_SESSION_PREFIX + (await sha256Hex(token))));
}

export async function revokeFleetSession(request: Request, env: Env): Promise<void> {
  const token = requestCookie(request, "fleet_session");
  if (token) await env.KV.delete(KV_SESSION_PREFIX + (await sha256Hex(token)));
}

export function sameOriginRequest(request: Request): boolean {
  const origin = request.headers.get("Origin");
  if (!origin) return true;
  return origin === new URL(request.url).origin;
}

export async function requireFleetAuth(request: Request, env: Env): Promise<Response | null> {
  if (!(await fleetPasswordConfigured(env.KV, env))) {
    return json({ error: "setup required", needs_password: true }, 403);
  }
  if (!sameOriginRequest(request)) {
    return json({ error: "cross-origin request rejected" }, 403);
  }
  if (await fleetSessionValid(request, env)) return null;
  const header = request.headers.get("X-Fleet-Password") || "";
  if (header && (await verifyFleetPassword(env.KV, env, header)).ok) return null;
  return json({ error: "unauthorized" }, 401);
}


export async function encryptionKey(env: Env): Promise<CryptoKey | null> {
  if (!env.FLEET_ENC_KEY) return null;
  const raw = unb64(env.FLEET_ENC_KEY);
  if (raw.length !== 32) throw new Error("FLEET_ENC_KEY must be 32 bytes, base64 encoded");
  return crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
}

export async function protectSecret(value: string, env: Env): Promise<string> {
  const key = await encryptionKey(env);
  if (!key) return value;
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const data = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, new TextEncoder().encode(value));
  return `enc:${b64(iv)}.${b64(data)}`;
}

export async function revealSecret(value: string, env: Env): Promise<string> {
  if (!value.startsWith("enc:")) return value;
  const key = await encryptionKey(env);
  if (!key) throw new Error("server password is encrypted but FLEET_ENC_KEY is not set");
  const [ivB64, dataB64] = value.slice(4).split(".");
  const plain = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: unb64(ivB64) },
    key,
    unb64(dataB64),
  );
  return new TextDecoder().decode(plain);
}

