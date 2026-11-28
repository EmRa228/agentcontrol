"""Typed configuration and validation for the AgentControl panel."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SEVERITIES = ("info", "warning", "critical")
DEFAULT_NOTIFICATIONS_FILE = "/etc/agentcontrol/notifications.yaml"


class ConfigError(ValueError):
    pass


@dataclass
class Settings:
    """Typed view over the legacy config dict (mapping-compatible)."""

    port: int = 30228
    internal_port: int = 0
    https_internal_port: int = 30443
    bind_host: str = ""
    public_hostname: str = ""
    scan_root: str = "/root"
    state_dir: str = "/var/lib/agentcontrol"
    session_days: float = 30
    idle_release_seconds: int = 43200
    max_login_attempts: int = 10
    lockout_seconds: int = 300
    monitor_interval_seconds: int = 60
    metrics_interval_seconds: int = 15
    metrics_retention_hours: int = 168
    update_check_seconds: int = 21600
    disk_warn_percent: float = 85.0
    disk_crit_percent: float = 95.0
    cert_warn_days: int = 21
    cert_crit_days: int = 7
    terminal_enabled: bool = True
    status_page_enabled: bool = True
    metrics_public: bool = False
    notifications_file: str = DEFAULT_NOTIFICATIONS_FILE
    raw: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        if key in self.__dataclass_fields__ and key != "raw":
            return getattr(self, key)
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: str) -> bool:
        return key in self.raw or (key in self.__dataclass_fields__ and key != "raw")

    def __setitem__(self, key: str, value: Any) -> None:
        self.raw[key] = value
        if key in self.__dataclass_fields__ and key != "raw":
            setattr(self, key, value)

    def update(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self[key] = value

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def build_settings(cfg: dict[str, Any]) -> Settings:
    return Settings(
        port=_as_int(cfg.get("port"), 30228),
        internal_port=_as_int(cfg.get("internal_port"), 0),
        https_internal_port=_as_int(cfg.get("https_internal_port"), 30443),
        bind_host=str(cfg.get("bind_host") or ""),
        public_hostname=str(cfg.get("public_hostname") or ""),
        scan_root=str(cfg.get("scan_root") or "/root"),
        state_dir=str(cfg.get("state_dir") or "/var/lib/agentcontrol"),
        session_days=_as_float(cfg.get("session_days"), 30),
        idle_release_seconds=_as_int(cfg.get("idle_release_seconds"), 43200),
        max_login_attempts=_as_int(cfg.get("max_login_attempts"), 10),
        lockout_seconds=_as_int(cfg.get("lockout_seconds"), 300),
        monitor_interval_seconds=_as_int(cfg.get("monitor_interval_seconds"), 60),
        metrics_interval_seconds=_as_int(cfg.get("metrics_interval_seconds"), 15),
        metrics_retention_hours=_as_int(cfg.get("metrics_retention_hours"), 168),
        update_check_seconds=_as_int(cfg.get("update_check_seconds"), 21600),
        disk_warn_percent=_as_float(cfg.get("disk_warn_percent"), 85.0),
        disk_crit_percent=_as_float(cfg.get("disk_crit_percent"), 95.0),
        cert_warn_days=_as_int(cfg.get("cert_warn_days"), 21),
        cert_crit_days=_as_int(cfg.get("cert_crit_days"), 7),
        terminal_enabled=_as_bool(cfg.get("terminal_enabled"), True),
        status_page_enabled=_as_bool(cfg.get("status_page_enabled"), True),
        metrics_public=_as_bool(cfg.get("metrics_public"), False),
        notifications_file=str(
            cfg.get("notifications_file")
            or os.environ.get("NOTIFICATIONS_FILE", DEFAULT_NOTIFICATIONS_FILE)
        ),
        raw=dict(cfg),
    )


def validate_settings(settings: Settings) -> list[str]:
    """Return human-readable problems; empty means valid."""
    problems: list[str] = []
    for key in ("port", "internal_port", "https_internal_port"):
        value = _as_int(getattr(settings, key), 0)
        if value and not (1 <= value <= 65535):
            problems.append(f"{key} must be between 1 and 65535 (got {value})")
    if settings.internal_port and settings.internal_port == settings.port:
        problems.append("internal_port must differ from port")
    if settings.internal_port and settings.https_internal_port == settings.internal_port:
        problems.append("https_internal_port must differ from internal_port")
    if settings.bind_host not in {"", "127.0.0.1", "localhost", "0.0.0.0", "::1"}:
        problems.append(f"bind_host looks invalid: {settings.bind_host!r}")
    if settings.session_days <= 0:
        problems.append("session_days must be > 0")
    if settings.idle_release_seconds <= 0:
        problems.append("idle_release_seconds must be > 0")
    if settings.max_login_attempts < 1:
        problems.append("max_login_attempts must be >= 1")
    if settings.lockout_seconds < 1:
        problems.append("lockout_seconds must be >= 1")
    if settings.disk_warn_percent <= 0 or settings.disk_crit_percent <= settings.disk_warn_percent:
        problems.append("disk thresholds must satisfy 0 < disk_warn_percent < disk_crit_percent")
    return problems


# ---------------------------------------------------------------------------
# Notifications


@dataclass
class Channel:
    enabled: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)


@dataclass
class NotificationConfig:
    enabled: bool = True
    min_severity: str = "warning"
    throttle_seconds: int = 900
    events: dict[str, bool] = field(default_factory=dict)
    channels: dict[str, Channel] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=dict)

    def channel(self, name: str) -> Channel:
        return self.channels.get(name) or Channel()

    def event_enabled(self, event: str) -> bool:
        return bool(self.events.get(event, True))

    def severity_allowed(self, severity: str) -> bool:
        try:
            return SEVERITIES.index(severity) >= SEVERITIES.index(self.min_severity)
        except ValueError:
            return True

    def to_dict(self, redact: bool = True) -> dict[str, Any]:
        channels: dict[str, Any] = {}
        for name, channel in self.channels.items():
            options = dict(channel.options)
            if redact:
                for secret in ("bot_token", "password", "secret", "token"):
                    if options.get(secret):
                        options[secret] = ""
                        options[f"{secret}_set"] = True
            channels[name] = {"enabled": channel.enabled, **options}
        return {
            "enabled": self.enabled,
            "min_severity": self.min_severity,
            "throttle_seconds": self.throttle_seconds,
            "events": dict(self.events),
            "thresholds": dict(self.thresholds),
            "channels": channels,
        }


DEFAULT_EVENTS = {
    "worker_down": True,
    "worker_error": True,
    "disk": True,
    "cert": True,
    "update": True,
    "opencode": True,
    "login_fail": False,
    "backup": True,
    "recovery": True,
}


def _channel(name: str, raw: Any) -> Channel:
    if isinstance(raw, bool):
        return Channel(enabled=raw)
    if not isinstance(raw, dict):
        return Channel()
    options = dict(raw)
    enabled = _as_bool(options.pop("enabled", False), False)
    return Channel(enabled=enabled, options=options)


def load_notifications(path: str | Path | None = None) -> NotificationConfig:
    target = Path(path or os.environ.get("NOTIFICATIONS_FILE", DEFAULT_NOTIFICATIONS_FILE))
    raw: dict[str, Any] = {}
    if target.is_file():
        try:
            raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    events = dict(DEFAULT_EVENTS)
    events.update({str(k): bool(v) for k, v in (raw.get("events") or {}).items()})
    channels = {
        name: _channel(name, raw.get("channels", {}).get(name, {}))
        for name in ("telegram", "webhook", "email", "ntfy")
    }
    # Allow flat channel dictionaries as well (telegram: {...}).
    for name in ("telegram", "webhook", "email", "ntfy"):
        if name in raw and name not in (raw.get("channels") or {}):
            channels[name] = _channel(name, raw[name])
    severity = str(raw.get("min_severity") or "warning").lower()
    if severity not in SEVERITIES:
        severity = "warning"
    return NotificationConfig(
        enabled=_as_bool(raw.get("enabled"), True),
        min_severity=severity,
        throttle_seconds=_as_int(raw.get("throttle_seconds"), 900),
        events=events,
        channels=channels,
        thresholds={
            "disk_warn": _as_float((raw.get("thresholds") or {}).get("disk_warn"), 85.0),
            "disk_crit": _as_float((raw.get("thresholds") or {}).get("disk_crit"), 95.0),
            "cert_warn_days": _as_float((raw.get("thresholds") or {}).get("cert_warn_days"), 21.0),
            "cert_crit_days": _as_float((raw.get("thresholds") or {}).get("cert_crit_days"), 7.0),
        },
    )


def save_notifications(config: NotificationConfig, path: str | Path | None = None) -> Path:
    target = Path(path or os.environ.get("NOTIFICATIONS_FILE", DEFAULT_NOTIFICATIONS_FILE))
    payload = config.to_dict(redact=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target
