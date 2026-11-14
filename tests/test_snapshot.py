"""snapshot.py tests against the local sandbox (python -m sandbox.ctl start first).

    python -m tests.test_snapshot
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp())
ENV = {**os.environ, "CAREEM_TARGET": "sandbox", "DB_PATH": str(TMP / "t.db")}

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {what}")
    if not cond:
        failures.append(what)


def run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "scripts/snapshot.py", *args], cwd=ROOT, env=env or ENV,
                          capture_output=True, text=True, timeout=120)


def force(tool: str | None, scenario: str | None = None) -> None:
    p = ROOT / "sandbox" / ".run" / "overrides.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({tool: scenario} if tool else {}))


def main() -> int:
    force(None)
    posts = TMP / "posts.json"
    posts.write_text(json.dumps({"posts": [
        {"id": "shawarma", "caption": "Best shawarma wrap in the Marina", "keys": ["marina wrap"], "media_ids": ["M1"]},
        {"id": "pizza", "caption": "Pepperoni pizza at Pizza Piazza", "media_ids": []},
        {"id": "nothing", "caption": "xyz special", "media_ids": []},
    ]}))
    out = TMP / "catalog.json"

    r = run("--posts", str(posts), "--out", str(out))
    check(r.returncode == 2 and "refuses" in r.stdout, "refuses sandbox data without --allow-sandbox")
    r = run("--posts", str(posts), "--allow-sandbox")
    check(r.returncode == 2 and "--out" in r.stdout, "refuses to write sandbox data into the real catalog.json")

    r = run("--posts", str(posts), "--out", str(out), "--allow-sandbox", "--method", "rules")
    cat = json.loads(out.read_text())
    e = cat["entries"]
    check(r.returncode == 0 and set(e) == {"shawarma", "pizza", "nothing"}, f"3 posts captured ({r.stdout.strip().splitlines()[-1]})")
    check(e["shawarma"]["result"]["item"]["name"] == "Chicken Shawarma Wrap" and e["shawarma"]["captured_by"] == "rules",
          "shawarma entry has the real sandbox dish")
    check("marina wrap" in e["shawarma"]["keys"] and "shawarma station" in e["shawarma"]["keys"],
          f"keys = post keys + dish + merchant {e['shawarma']['keys']}")
    check(e["nothing"]["result"]["status"] == "none", "'not on Careem' post captured as none (missed-demand demo)")

    # read back through the CACHED rung
    os.environ.update({"CAREEM_TARGET": "sandbox", "DB_PATH": str(TMP / "t.db"), "CATALOG_PATH": str(out)})
    from app import fallbacks
    from app.contracts import Signal
    fallbacks.CATALOG_PATH = out
    s = Signal(id="x", platform="instagram", kind="dm_share", source_id="1", user_ref="u", text="", media_id="M1")
    res = fallbacks.cached_resolve(s)
    check(res.mode == "cached" and res.item.name == "Chicken Shawarma Wrap"
          and e["shawarma"]["captured_at"] in res.steps[0], f"CACHED by media_id with capture time ({res.steps[0]})")
    s = Signal(id="y", platform="instagram", kind="comment", source_id="2", user_ref="u", text="how much is the pepperoni pizza?")
    check(fallbacks.cached_resolve(s).item.name == "Pepperoni Pizza", "CACHED by keyword from a comment")

    # Careem down during a re-snapshot: old entries are kept, exit code 1
    before = json.loads(out.read_text())
    time.sleep(1.1)
    force("food_dish_search", "upstream-unavailable")
    r = run("--posts", str(posts), "--out", str(out), "--allow-sandbox", "--method", "rules", "--only", "shawarma")
    force(None)
    after = json.loads(out.read_text())
    check(r.returncode == 1 and after["entries"]["shawarma"] == before["entries"]["shawarma"],
          "failed re-snapshot keeps the previous good entry")

    r = run("--list", "--out", str(out))
    check(r.returncode == 0 and "3 entries" in r.stdout, "--list shows the catalog")

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
