"""Audit trail helpers."""

from __future__ import annotations

import json
from typing import Any

try:
    from flask import g, has_request_context, request
except Exception:  # pragma: no cover
    g = None
    request = None

    def has_request_context() -> bool:
        return False


def _current() -> dict[str, str]:
    if not has_request_context() or g is None:
        return {"actor": "system", "role": "system", "ip": "", "ua": "", "source": "system"}
    return {
        "actor": getattr(g, "actor_name", "panel"),
        "role": getattr(g, "actor_role", "admin"),
        "ip": (request.remote_addr or "") if request else "",
        "ua": (request.headers.get("User-Agent", "") if request else ""),
        "source": getattr(g, "actor_source", "panel"),
    }


def record(
    store,
    action: str,
    *,
    target: str = "",
    detail: Any = "",
    actor: str | None = None,
    role: str | None = None,
    source: str | None = None,
) -> None:
    if store is None:
        return
    context = _current()
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
    try:
        store.audit_add(
            action,
            actor=actor if actor is not None else context["actor"],
            role=role if role is not None else context["role"],
            ip=context["ip"],
            ua=context["ua"],
            target=target,
            detail=str(detail),
            source=source if source is not None else context["source"],
        )
    except Exception:
        pass
