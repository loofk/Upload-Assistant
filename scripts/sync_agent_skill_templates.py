#!/usr/bin/env python3
"""Write or verify static AI skill manifests from the ptcli service contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ptcli.service import agent_manifest_payload  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
SKILL_TEMPLATE_PATHS = (
    ROOT / "ai" / "openclaw" / "ptcli.skill.json",
    ROOT / "ai" / "hermes" / "ptcli.skill.json",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync static OpenClaw/Hermes skill manifests with the live ptcli agent manifest schema.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Base URL written into static manifests. Default: {DEFAULT_BASE_URL}")
    parser.add_argument("--write", action="store_true", help="Rewrite static manifest files. Without this flag the script only checks for drift.")
    args = parser.parse_args(argv)

    expected = json.dumps(agent_manifest_payload(base_url=args.base_url), ensure_ascii=False, indent=2) + "\n"
    stale_paths: list[Path] = []
    for path in SKILL_TEMPLATE_PATHS:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == expected:
            continue
        stale_paths.append(path)
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")

    if stale_paths and not args.write:
        for path in stale_paths:
            print(f"{path.relative_to(ROOT)} is out of date; run scripts/sync_agent_skill_templates.py --write", file=sys.stderr)
        return 1
    if args.write:
        action = "updated" if stale_paths else "already current"
        print(f"Static ptcli agent skill templates {action}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
