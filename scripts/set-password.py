#!/usr/bin/env python3
"""Set the panel password (stored as an Argon2 hash).

Usage:
  scripts/set-password.py                 # prompt twice
  scripts/set-password.py --stdin         # read one line from stdin
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from panel.security import write_password_file  # noqa: E402

CONFIG_CANDIDATES = [
    Path(os.environ.get("CONFIG_FILE", "/etc/agentcontrol/config.yaml")),
    ROOT / "config.yaml",
    ROOT / "config.example.yaml",
]


def config_path_value(key: str, default: str) -> str:
    explicit = os.environ.get("CONFIG_FILE")
    candidates = [Path(explicit)] if explicit else CONFIG_CANDIDATES
    if explicit and not Path(explicit).is_file():
        raise SystemExit(f"CONFIG_FILE does not exist: {explicit}")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        value = data.get(key)
        if value:
            return str(value)
    return default


def main() -> int:
    password = ""
    if "--stdin" in sys.argv:
        password = sys.stdin.readline().strip()
    else:
        password = getpass.getpass("New panel password: ")
        confirm = getpass.getpass("Repeat: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 1
    target = Path(config_path_value("auth_password_file", "/etc/agentcontrol/auth-password"))
    write_password_file(target, password)
    print(f"Password hash written to {target}")
    print("All existing sessions remain valid; sign out all devices from Settings if needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
