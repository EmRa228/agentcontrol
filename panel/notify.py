"""Multi-channel notifications: Telegram, webhook, email, ntfy."""

from __future__ import annotations

import hashlib
import hmac
import json
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import SEVERITIES, NotificationConfig, load_notifications

HOSTNAME = socket.gethostname()
USER_AGENT = "AgentControl-Notifier/1"


class Notifier:
    def __init__(self, store, config_path: str | Path):
        self.store = store
        self.config_path = Path(config_path)
        self._config: NotificationConfig | None = None
        self._mtime = 0.0

    def config(self) -> NotificationConfig:
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if self._config is None or mtime != self._mtime:
            self._config = load_notifications(self.config_path)
            self._mtime = mtime
        return self._config

    def reload(self) -> NotificationConfig:
        self._config = None
        self._mtime = 0.0
        return self.config()

    # -- public API ------------------------------------------------------
    def notify(
        self,
        event: str,
        severity: str,
        title: str,
        message: str,
        *,
        key: str = "",
        detail: dict | None = None,
        force: bool = False,
    ) -> dict:
        if severity not in SEVERITIES:
            severity = "warning"
        config = self.config()
        incident_key = key or f"{event}:{title}"
        now = time.time()
        if self.store is not None:
            current = self.store.incident_get(incident_key)
            self.store.incident_upsert(
                incident_key,
                event,
                severity,
                title,
                json.dumps(detail or {}, ensure_ascii=False),
                state="open",
            )
            if (
                not force
                and current
                and current.get("last_notified_ts")
                and now - float(current["last_notified_ts"]) < config.throttle_seconds
            ):
                return {"sent": False, "reason": "throttled", "incidents": [incident_key]}
        if not force:
            if not config.enabled:
                return {"sent": False, "reason": "disabled"}
            if not config.event_enabled(event):
                return {"sent": False, "reason": "event_disabled"}
            if not config.severity_allowed(severity):
                return {"sent": False, "reason": "below_threshold"}
        results = self._send_all(event, severity, title, message, detail or {}, force=force)
        if any(result.get("ok") for result in results) and self.store is not None:
            self.store.incident_mark_notified(incident_key)
        return {"sent": any(r.get("ok") for r in results), "results": results, "incidents": [incident_key]}

    def resolve(self, key: str, title: str = "Recovered", message: str = "") -> dict:
        config = self.config()
        incident = self.store.incident_get(key) if self.store else None
        if not incident or incident.get("state") == "resolved":
            return {"sent": False, "reason": "not_open"}
        if self.store:
            self.store.incident_upsert(key, incident["event"], "info", title, "", state="resolved")
        if config.event_enabled("recovery"):
            return self.notify(
                "recovery",
                "info",
                title,
                message or incident.get("message", ""),
                key=key,
                force=False,
            )
        return {"sent": False, "reason": "recovery_disabled"}

    def test_channel(self, name: str) -> dict:
        config = self.config()
        channel = config.channel(name)
        if not channel.enabled:
            return {"ok": False, "error": "channel is disabled"}
        return self._send_channel(name, channel.options, "info", "AgentControl test",
                                  f"Test notification from {HOSTNAME}.", {"test": True})

    # -- channels --------------------------------------------------------
    def _send_all(self, event, severity, title, message, detail, *, force: bool) -> list[dict]:
        config = self.config()
        results = []
        for name, channel in config.channels.items():
            if not channel.enabled:
                continue
            if not force and name == "email" and not channel.get("to"):
                continue
            results.append(
                self._send_channel(
                    name,
                    channel.options,
                    severity,
                    title,
                    message,
                    {"event": event, "host": HOSTNAME, **detail},
                )
            )
        return results

    def _send_channel(self, name: str, options: dict[str, Any], severity, title, message, detail) -> dict:
        try:
            if name == "telegram":
                return self._telegram(options, severity, title, message)
            if name == "webhook":
                return self._webhook(options, severity, title, message, detail)
            if name == "email":
                return self._email(options, severity, title, message)
            if name == "ntfy":
                return self._ntfy(options, severity, title, message)
            return {"channel": name, "ok": False, "error": "unknown channel"}
        except Exception as exc:
            return {"channel": name, "ok": False, "error": str(exc)[:300]}

    @staticmethod
    def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 10):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-configured URL
            return response.status, response.read(4096)

    def _telegram(self, options, severity, title, message) -> dict:
        token = str(options.get("bot_token") or "")
        chat_id = str(options.get("chat_id") or "")
        if not token or not chat_id:
            return {"channel": "telegram", "ok": False, "error": "bot_token/chat_id missing"}
        icon = {"critical": "🔴", "warning": "🟠"}.get(severity, "ℹ️")
        text = f"{icon} <b>{_html(title)}</b>\n{_html(message)}\n\n<i>{_html(HOSTNAME)}</i>"
        status, _ = self._post_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        )
        return {"channel": "telegram", "ok": 200 <= status < 300, "status": status}

    @staticmethod
    def _post_raw(url: str, body: bytes, headers: dict | None = None, timeout: int = 10):
        request = Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-configured URL
            return response.status, response.read(4096)

    def _webhook(self, options, severity, title, message, detail) -> dict:
        url = str(options.get("url") or "")
        if not url:
            return {"channel": "webhook", "ok": False, "error": "url missing"}
        payload = {
            "event": detail.get("event", "alert"),
            "severity": severity,
            "title": title,
            "message": message,
            "host": HOSTNAME,
            "ts": int(time.time()),
            "detail": detail,
        }
        headers = {str(k): str(v) for k, v in (options.get("headers") or {}).items()}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        secret = str(options.get("secret") or "")
        if secret:
            headers["X-AgentControl-Signature"] = "sha256=" + hmac.new(
                secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
        status, _ = self._post_raw(url, body, headers)
        return {"channel": "webhook", "ok": 200 <= status < 300, "status": status}

    def _email(self, options, severity, title, message) -> dict:
        host = str(options.get("smtp_host") or "")
        to = options.get("to") or []
        if isinstance(to, str):
            to = [part.strip() for part in to.split(",") if part.strip()]
        if not host or not to:
            return {"channel": "email", "ok": False, "error": "smtp_host/to missing"}
        port = int(options.get("smtp_port") or 587)
        sender = str(options.get("from") or options.get("username") or "agentcontrol@localhost")
        password = str(options.get("password") or "")
        if not password and options.get("password_file"):
            try:
                password = Path(str(options["password_file"])).read_text(encoding="utf-8").strip()
            except OSError:
                password = ""
        msg = EmailMessage()
        msg["Subject"] = f"[AgentControl] {severity}: {title}"
        msg["From"] = sender
        msg["To"] = ", ".join(to)
        msg.set_content(f"{message}\n\nHost: {HOSTNAME}\nSeverity: {severity}\n")
        if options.get("use_ssl", True):
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=12, context=context) as smtp:
                if options.get("username"):
                    smtp.login(str(options["username"]), password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=12) as smtp:
                smtp.ehlo()
                if options.get("starttls", True):
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if options.get("username"):
                    smtp.login(str(options["username"]), password)
                smtp.send_message(msg)
        return {"channel": "email", "ok": True}

    def _ntfy(self, options, severity, title, message) -> dict:
        base = str(options.get("url") or "https://ntfy.sh").rstrip("/")
        topic = str(options.get("topic") or "")
        if not topic:
            return {"channel": "ntfy", "ok": False, "error": "topic missing"}
        priority = {"critical": "5", "warning": "4"}.get(severity, "3")
        request = Request(f"{base}/{quote(topic)}", data=message.encode("utf-8"), method="POST")
        request.add_header("Title", title.encode("ascii", "replace").decode("ascii"))
        request.add_header("Priority", priority)
        request.add_header("Tags", "robot")
        request.add_header("User-Agent", USER_AGENT)
        token = str(options.get("token") or "")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urlopen(request, timeout=10) as response:  # noqa: S310
            return {"channel": "ntfy", "ok": 200 <= response.status < 300, "status": response.status}


def _html(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
