"""[P1] Print every Careem MCP tool, flag mutating ones with ⚠️, and check our allowlists.

    python scripts/list_tools.py                     # target from CAREEM_TARGET (default production)
    python scripts/list_tools.py --target sandbox
    python scripts/list_tools.py --json              # raw definitions
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import careem_mcp  # noqa: E402
from app.careem_mcp import CODE_ONLY, MUTATING, NEVER, READ_ONLY  # noqa: E402


def role(name: str) -> str:
    if name in NEVER:
        return "NEVER"
    if name in READ_ONLY:
        return "agent"
    if name in CODE_ONLY:
        return "code-only"
    return "-"


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser()
    p.add_argument("--target", choices=["production", "sandbox"])
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    client = careem_mcp.get(a.target)
    try:
        tools = sorted(client.list_tools(), key=lambda t: t["name"])
    except Exception as e:  # noqa: BLE001
        print(f"❌ could not list tools on {client.target} ({client.url}): {type(e).__name__}: {e}")
        return 1
    if a.json:
        print(json.dumps(tools, indent=2, ensure_ascii=False))
        return 0

    print(f"{len(tools)} tools on {client.target} ({client.url})\n")
    print(f"   {'tool':40} {'our role':10} title")
    for t in tools:
        ro = (t.get("annotations") or {}).get("readOnlyHint") is True
        mutating = not ro or t["name"] in MUTATING or t["name"] in NEVER
        print(f"{'⚠️ ' if mutating else '   '}{t['name']:40} {role(t['name']):10} {t.get('title', '')}")

    names = {t["name"] for t in tools}
    problems = []
    for n in sorted(READ_ONLY | CODE_ONLY):
        if n not in names:
            problems.append(f"allowlisted tool {n} is not on the server")
    for t in tools:
        if t["name"] in READ_ONLY and (t.get("annotations") or {}).get("readOnlyHint") is not True:
            problems.append(f"{t['name']} is in READ_ONLY but the server does not mark it read-only")
    print()
    if problems:
        print("Allowlist problems:\n  - " + "\n  - ".join(problems))
        return 1
    print(f"✅ allowlists OK: {len(READ_ONLY)} agent tools, {len(CODE_ONLY)} code-only tools, "
          f"{len(names & NEVER)} never-tools present and blocked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
