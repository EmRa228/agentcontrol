#!/usr/bin/env python3
"""CLI helper to apply AgentControl xray client settings."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xray_client import (  # noqa: E402
    apply_client_settings,
    import_from_xray_config,
    load_client_settings,
    merge_vless_url,
    public_settings,
    save_client_settings,
    test_proxy,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply AgentControl xray client config")
    parser.add_argument("--import", dest="do_import", action="store_true", help="Import from xray config.json")
    parser.add_argument("--show", action="store_true", help="Print current settings")
    parser.add_argument("--test", action="store_true", help="Test HTTP proxy")
    parser.add_argument("--no-restart", action="store_true", help="Do not restart xray")
    parser.add_argument("--json", dest="json_path", help="Apply settings from JSON file")
    parser.add_argument("--vless-url", dest="vless_url", help="Apply settings from a vless:// share link")
    args = parser.parse_args()

    if args.vless_url:
        settings = merge_vless_url(load_client_settings(), args.vless_url)
        settings["enabled"] = True
        result = apply_client_settings(settings, restart=not args.no_restart)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    if args.do_import:
        imported = import_from_xray_config()
        if not imported:
            print("Could not import from xray config", file=sys.stderr)
            return 1
        save_client_settings(imported)
        print(json.dumps(public_settings(imported), indent=2))
        return 0

    if args.show:
        print(json.dumps(public_settings(load_client_settings()), indent=2))
        return 0

    if args.test:
        print(json.dumps(test_proxy(), indent=2))
        return 0

    if args.json_path:
        data = json.loads(Path(args.json_path).read_text(encoding="utf-8"))
        result = apply_client_settings(data, restart=not args.no_restart)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    result = apply_client_settings(load_client_settings(), restart=not args.no_restart)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
