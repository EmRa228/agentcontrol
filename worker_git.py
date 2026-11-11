"""Git helpers for Cursor worker directories (repo label registration)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

GIT_REMOTE_MARKER = ".agentcontrol-git-remote"


def _git(path: Path, args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def git_remote_url(path: Path) -> str | None:
    if not (path / ".git").exists():
        return None
    proc = _git(path, ["remote", "get-url", "origin"])
    if proc.returncode != 0:
        return None
    url = (proc.stdout or "").strip()
    return url or None


def folder_git_status(path: Path) -> dict[str, Any]:
    is_repo = (path / ".git").is_dir()
    remote = git_remote_url(path) if is_repo else None
    return {
        "is_repo": is_repo,
        "has_remote": bool(remote),
        "remote_url": remote,
    }


def _read_marker_remote(path: Path) -> str | None:
    marker = path / GIT_REMOTE_MARKER
    if not marker.is_file():
        return None
    for line in marker.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            return text
    return None


def resolve_git_remote(name: str, path: Path, cfg: dict) -> str | None:
    from_marker = _read_marker_remote(path)
    if from_marker:
        return from_marker
    overrides = cfg.get("git_remote_overrides") or {}
    if isinstance(overrides, dict):
        override = overrides.get(name)
        if override:
            text = str(override).strip()
            if text:
                return text
    template = (cfg.get("git_remote_template") or "").strip()
    if template:
        try:
            return template.format(name=name, folder=name)
        except (KeyError, ValueError):
            return template.replace("{name}", name)
    return None


def _has_commits(path: Path) -> bool:
    return _git(path, ["rev-parse", "--verify", "HEAD"]).returncode == 0


def ensure_worker_git(name: str, path: Path, cfg: dict) -> dict[str, Any]:
    """Ensure .git exists (empty placeholder commit) and optional origin for Cursor repo label."""
    result: dict[str, Any] = {
        "initialized": False,
        "empty_commit": False,
        "remote_added": False,
        "remote_url": None,
        "skipped": False,
    }
    if not path.is_dir():
        result["skipped"] = True
        return result

    if not (path / ".git").exists():
        init = _git(path, ["init", "-b", "main"])
        if init.returncode != 0:
            result["init_error"] = (init.stderr or init.stdout or "git init failed").strip()
            return result
        result["initialized"] = True

    if not _has_commits(path):
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "agentcontrol",
            "GIT_AUTHOR_EMAIL": "agentcontrol@localhost",
            "GIT_COMMITTER_NAME": "agentcontrol",
            "GIT_COMMITTER_EMAIL": "agentcontrol@localhost",
        }
        commit = subprocess.run(
            ["git", "-C", str(path), "commit", "--allow-empty", "-m", "agentcontrol: placeholder"],
            capture_output=True,
            text=True,
            env=env,
        )
        if commit.returncode == 0:
            result["empty_commit"] = True

    existing = git_remote_url(path)
    if existing:
        result["remote_url"] = existing
        return result

    remote = resolve_git_remote(name, path, cfg)
    if remote:
        add = _git(path, ["remote", "add", "origin", remote])
        if add.returncode == 0:
            result["remote_added"] = True
            result["remote_url"] = remote
        else:
            result["remote_error"] = (add.stderr or add.stdout or "git remote add failed").strip()

    return result
