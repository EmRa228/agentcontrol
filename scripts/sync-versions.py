#!/usr/bin/env python3
"""Keep every version file in sync with the root version.json.

Usage: scripts/sync-versions.py [--check]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    check = "--check" in sys.argv
    root = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
    version = str(root["version"])
    updated = str(root.get("updated") or "")
    problems = []

    targets = {
        ROOT / "fleet" / "version.json": {"component": "fleet", "version": version, "updated": updated},
        ROOT / "fleet" / "public" / "version.json": {"component": "fleet", "version": version, "updated": updated},
    }
    for path, payload in targets.items():
        current = {}
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {}
        if current != payload:
            problems.append(str(path.relative_to(ROOT)))
            if not check:
                path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    package = ROOT / "fleet" / "package.json"
    if package.is_file():
        data = json.loads(package.read_text(encoding="utf-8"))
        if data.get("version") != version:
            problems.append(str(package.relative_to(ROOT)))
            if not check:
                data["version"] = version
                package.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    lock = ROOT / "fleet" / "package-lock.json"
    if lock.is_file():
        data = json.loads(lock.read_text(encoding="utf-8"))
        changed = False
        if data.get("version") != version:
            data["version"] = version
            changed = True
        if isinstance(data.get("packages"), dict) and data["packages"].get(""):
            if data["packages"][""].get("version") != version:
                data["packages"][""]["version"] = version
                changed = True
        if changed:
            problems.append(str(lock.relative_to(ROOT)))
            if not check:
                lock.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    if check and problems:
        print("Version drift in: " + ", ".join(problems), file=sys.stderr)
        return 1
    print(f"Versions synced to {version}" + (f" (updated {', '.join(problems)})" if problems else " (no changes)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
