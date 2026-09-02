"""Multi-proxy pool with subscription fetch, health checks, and automatic failover."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

import yaml

from xray_client import (
    apply_client_settings,
    apply_proxy_env,
    load_client_settings,
    merge_share_url,
    parse_share_url,
    proxy_listening,
    proxy_url,
    public_settings,
    restart_xray,
    save_client_settings,
    test_cursor_api,
    test_proxy,
)

POOL_FILE = Path(os.environ.get("PROXY_POOL_FILE", "/etc/agentcontrol/proxy-pool.yaml"))
POOL_STATUS_FILE = Path(
    os.environ.get("PROXY_POOL_STATUS_FILE", "/var/lib/agentcontrol/proxy-pool-status.json")
)
MAX_LOG_ENTRIES = 80
CURSOR_TEST_URL = "https://api2.cursor.sh"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_pool() -> dict[str, Any]:
    return {
        "enabled": False,
        "subscriptions": [],
        "proxies": [],
        "active_proxy_id": "",
        "last_check_at": None,
        "last_selection": None,
        "logs": [],
    }


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def load_pool() -> dict[str, Any]:
    pool = default_pool()
    if POOL_FILE.is_file():
        try:
            with open(POOL_FILE, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            pool.update(raw)
        except (OSError, yaml.YAMLError):
            pass
    pool.setdefault("subscriptions", [])
    pool.setdefault("proxies", [])
    pool.setdefault("logs", [])
    return pool


def save_pool(pool: dict[str, Any]) -> dict[str, Any]:
    POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(POOL_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(pool, f, default_flow_style=False, sort_keys=False)
    os.chmod(POOL_FILE, 0o600)
    return pool


def append_log(pool: dict[str, Any], level: str, message: str, **extra: Any) -> None:
    entry: dict[str, Any] = {"at": _utc_now(), "level": level, "message": message}
    entry.update(extra)
    logs = pool.setdefault("logs", [])
    logs.insert(0, entry)
    del logs[MAX_LOG_ENTRIES:]
    save_pool(pool)
    _write_status(pool)


def _write_status(pool: dict[str, Any] | None = None) -> dict[str, Any]:
    pool = pool or load_pool()
    active = get_active_proxy(pool)
    status = {
        "at": _utc_now(),
        "enabled": bool(pool.get("enabled")),
        "active_proxy_id": pool.get("active_proxy_id") or "",
        "active_proxy": public_proxy_entry(active) if active else None,
        "proxy_count": len(pool.get("proxies") or []),
        "subscription_count": len(pool.get("subscriptions") or []),
        "last_check_at": pool.get("last_check_at"),
        "last_selection": pool.get("last_selection"),
        "listening": proxy_listening() if pool.get("enabled") else False,
        "proxy_url": proxy_url() if pool.get("enabled") else None,
        "xray_settings": public_settings(load_client_settings()),
        "recent_logs": (pool.get("logs") or [])[:20],
    }
    POOL_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    POOL_STATUS_FILE.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    return status


def public_proxy_entry(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entry:
        return None
    check = entry.get("last_check") or {}
    share = entry.get("share_url") or ""
    masked = share[:24] + "…" if len(share) > 28 else share
    return {
        "id": entry.get("id"),
        "name": entry.get("name") or entry.get("id"),
        "enabled": bool(entry.get("enabled", True)),
        "source": entry.get("source") or "manual",
        "subscription_id": entry.get("subscription_id") or "",
        "share_preview": masked,
        "last_check": check,
        "protocol": entry.get("protocol"),
        "address": entry.get("address"),
        "port": entry.get("port"),
    }


def list_public_proxies(pool: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    pool = pool or load_pool()
    return [public_proxy_entry(p) or {} for p in pool.get("proxies") or []]


def get_proxy_by_id(pool: dict[str, Any], proxy_id: str) -> dict[str, Any] | None:
    for item in pool.get("proxies") or []:
        if item.get("id") == proxy_id:
            return item
    return None


def get_active_proxy(pool: dict[str, Any] | None = None) -> dict[str, Any] | None:
    pool = pool or load_pool()
    active_id = pool.get("active_proxy_id") or ""
    if active_id:
        found = get_proxy_by_id(pool, active_id)
        if found:
            return found
    proxies = [p for p in pool.get("proxies") or [] if p.get("enabled", True)]
    return proxies[0] if proxies else None


def _parse_subscription_body(body: str) -> list[str]:
    text = body.strip()
    if not text:
        return []
    links: list[str] = []
    if "://" not in text and re.fullmatch(r"[A-Za-z0-9+/=_-]+", text.replace("\n", "")):
        try:
            decoded = base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", errors="ignore")
            text = decoded
        except (ValueError, UnicodeDecodeError):
            pass
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            links.append(line)
    return links


def fetch_subscription(url: str, timeout: int = 25) -> tuple[list[str], str | None]:
    settings = load_client_settings()
    env_proxy = apply_proxy_env(os.environ.copy()) if settings.get("enabled") else os.environ.copy()
    proxy = env_proxy.get("HTTPS_PROXY") or env_proxy.get("HTTP_PROXY")
    try:
        if proxy:
            opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
            with opener.open(url, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
        else:
            with urlopen(url, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
        return _parse_subscription_body(body), None
    except (URLError, OSError, TimeoutError) as exc:
        return [], str(exc)


def refresh_subscription(pool: dict[str, Any], sub_id: str) -> dict[str, Any]:
    sub = next((s for s in pool.get("subscriptions") or [] if s.get("id") == sub_id), None)
    if not sub:
        raise ValueError(f"subscription not found: {sub_id}")
    url = str(sub.get("url") or "").strip()
    if not url:
        raise ValueError("subscription URL is empty")

    links, error = fetch_subscription(url)
    sub["last_fetch_at"] = _utc_now()
    if error:
        sub["last_error"] = error
        append_log(pool, "error", f"Subscription fetch failed: {sub.get('name') or sub_id}", error=error)
        save_pool(pool)
        return sub

    sub["last_error"] = ""
    sub["last_link_count"] = len(links)
    existing_urls = {p.get("share_url") for p in pool.get("proxies") or []}
    added = 0
    for link in links:
        if link in existing_urls:
            continue
        try:
            parsed = parse_share_url(link)
        except ValueError:
            continue
        entry = {
            "id": _new_id(),
            "name": parsed.get("address") or f"sub-{sub_id[:6]}",
            "share_url": link,
            "enabled": True,
            "source": "subscription",
            "subscription_id": sub_id,
            "protocol": parsed.get("protocol"),
            "address": parsed.get("address"),
            "port": parsed.get("port"),
            "last_check": None,
        }
        pool.setdefault("proxies", []).append(entry)
        existing_urls.add(link)
        added += 1

    append_log(
        pool,
        "info",
        f"Subscription refreshed: {sub.get('name') or sub_id} (+{added} proxies, {len(links)} links)",
        added=added,
        total_links=len(links),
    )
    save_pool(pool)
    return sub


def refresh_all_subscriptions(pool: dict[str, Any] | None = None) -> dict[str, Any]:
    pool = pool or load_pool()
    for sub in pool.get("subscriptions") or []:
        try:
            refresh_subscription(pool, sub["id"])
        except ValueError as exc:
            append_log(pool, "warn", str(exc))
    return pool


def add_subscription(pool: dict[str, Any], url: str, name: str = "") -> dict[str, Any]:
    url = url.strip()
    if not url:
        raise ValueError("subscription URL required")
    entry = {
        "id": _new_id(),
        "name": name.strip() or url[:48],
        "url": url,
        "added_at": _utc_now(),
        "last_fetch_at": None,
        "last_error": "",
        "last_link_count": 0,
    }
    pool.setdefault("subscriptions", []).append(entry)
    save_pool(pool)
    refresh_subscription(pool, entry["id"])
    return entry


def add_proxy_link(pool: dict[str, Any], share_url: str, name: str = "") -> dict[str, Any]:
    share_url = share_url.strip()
    if not share_url:
        raise ValueError("share_url required")
    parsed = parse_share_url(share_url)
    for item in pool.get("proxies") or []:
        if item.get("share_url") == share_url:
            return item
    entry = {
        "id": _new_id(),
        "name": name.strip() or str(parsed.get("address") or "proxy"),
        "share_url": share_url,
        "enabled": True,
        "source": "manual",
        "subscription_id": "",
        "protocol": parsed.get("protocol"),
        "address": parsed.get("address"),
        "port": parsed.get("port"),
        "last_check": None,
    }
    pool.setdefault("proxies", []).append(entry)
    save_pool(pool)
    append_log(pool, "info", f"Added proxy {entry['name']}")
    return entry


def apply_proxy_entry(entry: dict[str, Any], *, restart: bool = True) -> dict[str, Any]:
    current = load_client_settings()
    merged = merge_share_url(current, entry["share_url"])
    merged["enabled"] = True
    return apply_client_settings(merged, restart=restart, write_env=True)


def probe_proxy_entry(entry: dict[str, Any], *, apply: bool = True) -> dict[str, Any]:
    started = time.time()
    result: dict[str, Any] = {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "ok": False,
        "latency_ms": None,
        "cursor_api_ok": False,
        "error": "",
        "at": _utc_now(),
    }
    try:
        if apply:
            apply_proxy_entry(entry, restart=True)
            if not proxy_listening():
                result["error"] = "local proxy port not listening after apply"
                return result
        proxy_test = test_proxy(url="https://cursor.com")
        api_test = test_cursor_api()
        elapsed = int((time.time() - started) * 1000)
        result["latency_ms"] = elapsed
        result["cursor_test"] = proxy_test
        result["cursor_api_test"] = api_test
        result["ok"] = bool(proxy_test.get("ok") and api_test.get("ok"))
        if not result["ok"]:
            parts = []
            if not proxy_test.get("ok"):
                parts.append(proxy_test.get("error") or "cursor.com failed")
            if not api_test.get("ok"):
                parts.append(api_test.get("error") or "api2.cursor.sh failed")
            result["error"] = "; ".join(parts)
    except (ValueError, FileNotFoundError, OSError) as exc:
        result["error"] = str(exc)
    entry["last_check"] = {
        "at": result["at"],
        "ok": result["ok"],
        "latency_ms": result.get("latency_ms"),
        "error": result.get("error") or "",
    }
    return result


def test_all_proxies(pool: dict[str, Any] | None = None, *, apply_each: bool = True) -> dict[str, Any]:
    pool = pool or load_pool()
    results: list[dict[str, Any]] = []
    candidates = [p for p in pool.get("proxies") or [] if p.get("enabled", True)]
    append_log(pool, "info", f"Testing {len(candidates)} proxies…")
    for entry in candidates:
        result = probe_proxy_entry(entry, apply=apply_each)
        results.append(result)
        level = "info" if result["ok"] else "warn"
        append_log(
            pool,
            level,
            f"Probe {entry.get('name')}: {'OK' if result['ok'] else 'FAIL'} ({result.get('latency_ms')}ms)",
            proxy_id=entry.get("id"),
            error=result.get("error") or "",
        )
    pool["last_check_at"] = _utc_now()
    save_pool(pool)
    working = [r for r in results if r.get("ok")]
    working.sort(key=lambda r: r.get("latency_ms") or 99999)
    return {
        "at": pool["last_check_at"],
        "tested": len(results),
        "working": len(working),
        "results": results,
        "best": working[0] if working else None,
    }


def select_and_apply_best(pool: dict[str, Any] | None = None) -> dict[str, Any]:
    pool = pool or load_pool()
    if not pool.get("enabled"):
        return {"ok": True, "reason": "proxy disabled", "direct": True}

    report = test_all_proxies(pool, apply_each=True)
    best = report.get("best")
    if best:
        pool["active_proxy_id"] = best["id"]
        pool["last_selection"] = {
            "at": _utc_now(),
            "proxy_id": best["id"],
            "name": best.get("name"),
            "latency_ms": best.get("latency_ms"),
            "reason": "best_latency",
        }
        save_pool(pool)
        append_log(pool, "info", f"Selected proxy {best.get('name')} ({best.get('latency_ms')}ms)")
        _write_status(pool)
        return {"ok": True, "selected": best, "report": report}

    append_log(pool, "warn", "No working proxy — refreshing subscriptions")
    refresh_all_subscriptions(pool)
    report = test_all_proxies(pool, apply_each=True)
    best = report.get("best")
    if best:
        pool["active_proxy_id"] = best["id"]
        pool["last_selection"] = {
            "at": _utc_now(),
            "proxy_id": best["id"],
            "name": best.get("name"),
            "latency_ms": best.get("latency_ms"),
            "reason": "best_after_subscription_refresh",
        }
        save_pool(pool)
        append_log(pool, "info", f"Selected proxy after refresh: {best.get('name')}")
        _write_status(pool)
        return {"ok": True, "selected": best, "report": report, "refreshed": True}

    pool["last_selection"] = {
        "at": _utc_now(),
        "proxy_id": "",
        "reason": "no_working_proxy",
    }
    save_pool(pool)
    _write_status(pool)
    return {
        "ok": False,
        "error": "No working proxy found. Add proxies or subscription links in Settings.",
        "report": report,
        "refreshed": True,
    }


def quick_check_active(pool: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fast check: if active proxy responds on cursor API without full pool scan."""
    pool = pool or load_pool()
    if not pool.get("enabled"):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    if not proxy_listening():
        return {"ok": False, "error": "proxy port not listening", "needs_failover": True}

    api_test = test_cursor_api()
    if api_test.get("ok"):
        pool["last_check_at"] = _utc_now()
        active = get_active_proxy(pool)
        if active:
            active["last_check"] = {
                "at": pool["last_check_at"],
                "ok": True,
                "latency_ms": api_test.get("elapsed_ms"),
                "error": "",
            }
        save_pool(pool)
        _write_status(pool)
        return {"ok": True, "api_test": api_test}

    return {"ok": False, "api_test": api_test, "needs_failover": True}


def ensure_proxy_ready(*, force_reselect: bool = False) -> dict[str, Any]:
    """Called before worker start / API key verify when proxy mode is on."""
    pool = load_pool()
    if not pool.get("enabled"):
        client = load_client_settings()
        if client.get("enabled"):
            pool["enabled"] = True
            save_pool(pool)
        else:
            return {"ok": True, "mode": "direct"}

    if not pool.get("proxies"):
        migrate_legacy_to_pool(pool)

    if not pool.get("proxies") and not pool.get("subscriptions"):
        return {
            "ok": False,
            "error": "Proxy enabled but no proxies configured. Add share links or subscriptions.",
        }

    if not force_reselect:
        quick = quick_check_active(pool)
        if quick.get("ok"):
            return {"ok": True, "mode": "proxy", "quick": quick}

    append_log(pool, "info", "Running proxy failover selection…")
    result = select_and_apply_best(pool)
    if result.get("ok"):
        return {"ok": True, "mode": "proxy", **result}
    return result


def migrate_legacy_to_pool(pool: dict[str, Any] | None = None) -> dict[str, Any]:
    pool = pool or load_pool()
    settings = load_client_settings()
    if not settings.get("enabled"):
        return pool
    share = settings.get("share_url") or ""
    if not share:
        try:
            from xray_client import build_share_url

            share = build_share_url(settings)
        except ValueError:
            share = ""
    if share:
        add_proxy_link(pool, share, name=settings.get("address") or "legacy")
        pool["enabled"] = True
        if not pool.get("active_proxy_id") and pool.get("proxies"):
            pool["active_proxy_id"] = pool["proxies"][0]["id"]
        save_pool(pool)
        append_log(pool, "info", "Migrated legacy xray-client.yaml proxy into pool")
    return pool


def set_pool_enabled(enabled: bool) -> dict[str, Any]:
    pool = load_pool()
    pool["enabled"] = bool(enabled)
    save_pool(pool)
    settings = load_client_settings()
    settings["enabled"] = bool(enabled)
    save_client_settings(settings)
    if enabled:
        migrate_legacy_to_pool(pool)
        result = ensure_proxy_ready(force_reselect=not pool.get("active_proxy_id"))
        if not result.get("ok"):
            from xray_client import write_runtime_env

            write_runtime_env(None)
            return {"ok": False, "enabled": True, "error": result.get("error"), "status": pool_status()}
        return {"ok": True, "enabled": True, "status": pool_status(), "selection": result}
    from xray_client import set_proxy_enabled

    set_proxy_enabled(False, restart=True)
    append_log(pool, "info", "Proxy pool disabled — direct mode")
    _write_status(pool)
    return {"ok": True, "enabled": False, "status": pool_status()}


def apply_pool_config(data: dict[str, Any]) -> dict[str, Any]:
    pool = load_pool()
    if "enabled" in data:
        return set_pool_enabled(bool(data["enabled"]))

    if data.get("subscriptions"):
        for sub in data["subscriptions"]:
            url = str(sub.get("url") or "").strip()
            name = str(sub.get("name") or "").strip()
            if url:
                add_subscription(pool, url, name=name)

    if data.get("proxies"):
        for item in data["proxies"]:
            url = str(item.get("share_url") or item.get("vless_url") or "").strip()
            name = str(item.get("name") or "").strip()
            if url:
                add_proxy_link(pool, url, name=name)

    if data.get("replace"):
        if data.get("subscriptions") is not None:
            pool["subscriptions"] = []
            for sub in data["subscriptions"]:
                url = str(sub.get("url") or "").strip()
                if url:
                    add_subscription(pool, url, name=str(sub.get("name") or ""))
        if data.get("proxies") is not None:
            pool["proxies"] = []
            for item in data["proxies"]:
                url = str(item.get("share_url") or "").strip()
                if url:
                    add_proxy_link(pool, url, name=str(item.get("name") or ""))

    save_pool(pool)
    if pool.get("enabled"):
        ensure_proxy_ready(force_reselect=True)
    return {"ok": True, "status": pool_status()}


def pool_status() -> dict[str, Any]:
    pool = load_pool()
    status = _write_status(pool)
    status["subscriptions"] = [
        {
            "id": s.get("id"),
            "name": s.get("name"),
            "url_preview": (s.get("url") or "")[:60],
            "last_fetch_at": s.get("last_fetch_at"),
            "last_error": s.get("last_error"),
            "last_link_count": s.get("last_link_count"),
        }
        for s in pool.get("subscriptions") or []
    ]
    status["proxies"] = list_public_proxies(pool)
    return status


def is_proxy_mode_enabled() -> bool:
    pool = load_pool()
    if pool.get("enabled"):
        return True
    return bool(load_client_settings().get("enabled"))


def cursor_urlopen(url: str, timeout: int = 20):
    """Open a URL — must use proxy when proxy mode is enabled (blocks direct Cursor traffic)."""
    if is_proxy_mode_enabled():
        env = apply_proxy_env(os.environ.copy())
        proxy = env.get("HTTPS_PROXY") or env.get("HTTP_PROXY")
        if not proxy:
            raise URLError("proxy mode enabled but no HTTP_PROXY configured")
        opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(url, timeout=timeout)
    return urlopen(url, timeout=timeout)
