export const KV_NOTIFY = "fleet_notifications";

export interface FleetNotifyConfig {
  enabled: boolean;
  min_severity: "info" | "warning" | "critical";
  fail_threshold: number;
  events: { down: boolean; recovered: boolean };
  channels: {
    webhook: { enabled: boolean; url: string; secret?: string };
    telegram: { enabled: boolean; bot_token: string; chat_id: string };
  };
}

export function defaultNotifyConfig(): FleetNotifyConfig {
  return {
    enabled: true,
    min_severity: "warning",
    fail_threshold: 2,
    events: { down: true, recovered: true },
    channels: {
      webhook: { enabled: false, url: "" },
      telegram: { enabled: false, bot_token: "", chat_id: "" },
    },
  };
}

export async function readNotifyConfig(kv: KVNamespace): Promise<FleetNotifyConfig> {
  const raw = await kv.get(KV_NOTIFY);
  if (!raw) return defaultNotifyConfig();
  try {
    const parsed = JSON.parse(raw) as Partial<FleetNotifyConfig>;
    const base = defaultNotifyConfig();
    return {
      ...base,
      ...parsed,
      events: { ...base.events, ...(parsed.events || {}) },
      channels: {
        webhook: { ...base.channels.webhook, ...(parsed.channels?.webhook || {}) },
        telegram: { ...base.channels.telegram, ...(parsed.channels?.telegram || {}) },
      },
    };
  } catch {
    return defaultNotifyConfig();
  }
}

export function publicNotifyConfig(config: FleetNotifyConfig): Record<string, unknown> {
  const webhook = config.channels.webhook;
  const telegram = config.channels.telegram;
  return {
    enabled: config.enabled,
    min_severity: config.min_severity,
    fail_threshold: config.fail_threshold,
    events: config.events,
    channels: {
      webhook: { enabled: webhook.enabled, url: webhook.url, secret_set: Boolean(webhook.secret) },
      telegram: { enabled: telegram.enabled, chat_id: telegram.chat_id, bot_token_set: Boolean(telegram.bot_token) },
    },
  };
}

export async function notifyFleet(
  config: FleetNotifyConfig,
  severity: "info" | "warning" | "critical",
  title: string,
  message: string,
): Promise<void> {
  if (!config.enabled) return;
  const levels = ["info", "warning", "critical"];
  if (levels.indexOf(severity) < levels.indexOf(config.min_severity)) return;
  const jobs: Promise<unknown>[] = [];
  const webhook = config.channels.webhook;
  if (webhook.enabled && webhook.url) {
    const payload = { severity, title, message, source: "fleet", ts: Math.floor(Date.now() / 1000) };
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (webhook.secret) {
      const key = await crypto.subtle.importKey(
        "raw",
        new TextEncoder().encode(webhook.secret),
        { name: "HMAC", hash: "SHA-256" },
        false,
        ["sign"],
      );
      const body = JSON.stringify(payload);
      const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
      headers["X-Fleet-Signature"] = `sha256=${[...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
      jobs.push(
        fetch(webhook.url, { method: "POST", headers, body }).catch(() => undefined),
      );
    } else {
      jobs.push(
        fetch(webhook.url, { method: "POST", headers, body: JSON.stringify(payload) }).catch(() => undefined),
      );
    }
  }
  const telegram = config.channels.telegram;
  if (telegram.enabled && telegram.bot_token && telegram.chat_id) {
    const icon = severity === "critical" ? "🔴" : severity === "warning" ? "🟠" : "ℹ️";
    jobs.push(
      fetch(`https://api.telegram.org/bot${telegram.bot_token}/sendMessage`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          chat_id: telegram.chat_id,
          text: `${icon} <b>${title}</b>\n${message}`,
          parse_mode: "HTML",
        }),
      }).catch(() => undefined),
    );
  }
  await Promise.allSettled(jobs);
}

