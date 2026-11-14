"""[P1] Snapshot REAL production answers for the demo posts -> fixtures/catalog.json (the CACHED fallback).

    python scripts/snapshot.py                      # every post in fixtures/demo_posts.json
    python scripts/snapshot.py --only xoco-bar-mousse-reel
    python scripts/snapshot.py --method rules       # skip Bedrock, use the RULES resolver
    python scripts/snapshot.py --list               # show what is in the catalog now

Each post runs the live agent (falls back to RULES if Bedrock fails) against PRODUCTION Careem.
Results are merged: a post that fails keeps its previous good snapshot. Read-only calls only.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts import Signal  # noqa: E402

POSTS_PATH = ROOT / "fixtures" / "demo_posts.json"
CATALOG_PATH = ROOT / "fixtures" / "catalog.json"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".catalog.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def post_signal(post: dict[str, Any]) -> Signal:
    media = [str(m) for m in post.get("media_ids") or []]
    return Signal(
        id=f"snapshot:{post['id']}", platform="instagram", kind=post.get("kind") or "comment",
        source_id=f"snapshot-{post['id']}-{time.time():.0f}", user_ref="snapshot",
        text=post.get("text") or "", caption=post.get("caption"), restaurant=post.get("restaurant"),
        creator=post.get("creator"), media_id=media[0] if media else None,
    )


def resolve_for_snapshot(sig: Signal, method: str) -> tuple[Any, str]:
    """Returns (AgentResult, how). Never uses CACHED/SCRIPTED — a snapshot must be fresh, real data."""
    from app import agent, fallbacks

    if method == "agent":
        try:
            return agent.run_agent(sig), "agent"
        except Exception as e:  # noqa: BLE001
            logging.warning("agent failed for %s (%s: %s) — trying RULES", sig.id, type(e).__name__, str(e)[:120])
    return fallbacks.rule_resolve(sig), "rules"


def entry_keys(post: dict[str, Any], sig: Signal, result: Any) -> list[str]:
    from app import fallbacks

    keys = {k.lower().strip() for k in post.get("keys") or [] if k}
    for k in (fallbacks.extract_dish(sig), fallbacks.extract_restaurant(sig), post.get("restaurant"),
              result.merchant.name if result.merchant else None, result.item.name if result.item else None):
        if k and len(k.strip()) >= 3:
            keys.add(k.lower().strip())
    return sorted(keys, key=len, reverse=True)


def snapshot(posts: list[dict[str, Any]], *, method: str, out: Path, target: str) -> int:
    catalog = load_json(out, {"snapshot_at": None, "entries": {}})
    catalog.setdefault("entries", {})
    ok = failed = 0
    for post in posts:
        sig = post_signal(post)
        t0 = time.monotonic()
        try:
            result, how = resolve_for_snapshot(sig, method)
        except Exception as e:  # noqa: BLE001
            failed += 1
            kept = "kept previous snapshot" if post["id"] in catalog["entries"] else "no previous snapshot"
            print(f"❌ {post['id']}: {type(e).__name__}: {str(e)[:150]} ({kept})")
            continue
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        catalog["entries"][post["id"]] = {
            "keys": entry_keys(post, sig, result),
            "media_ids": [str(m) for m in post.get("media_ids") or []],
            "captured_at": now, "captured_by": how, "target": target,
            "result": result.to_dict(),
        }
        ok += 1
        what = f"{result.item.name} @ {result.merchant.name} AED {result.item.price_aed}" if result.item else "-"
        print(f"✅ {post['id']}: {result.status} via {how} in {time.monotonic() - t0:.1f}s — {what}, "
              f"{len(result.alternatives)} alternatives")
    if ok:
        catalog["snapshot_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        catalog["target"] = target
        write_json_atomic(out, catalog)
    print(f"\n{ok} captured, {failed} failed -> {out}")
    return 0 if failed == 0 else 1


def list_catalog(path: Path) -> int:
    cat = load_json(path, {})
    entries = cat.get("entries") or {}
    print(f"{path} — snapshot_at {cat.get('snapshot_at')}, target {cat.get('target')}, {len(entries)} entries")
    for eid, e in entries.items():
        r = e.get("result") or {}
        item = (r.get("item") or {}).get("name")
        print(f"  {eid:32} {r.get('status'):8} {item!s:40} {e.get('captured_at')} via {e.get('captured_by')}"
              f"  keys={e.get('keys')[:4]}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", action="append", help="post id (repeatable)")
    p.add_argument("--method", choices=["agent", "rules"], default="agent")
    p.add_argument("--posts", type=Path, default=POSTS_PATH)
    p.add_argument("--out", type=Path, default=CATALOG_PATH)
    p.add_argument("--list", action="store_true")
    p.add_argument("--allow-sandbox", action="store_true", help="TESTS ONLY: snapshot sandbox data")
    a = p.parse_args()
    if a.list:
        return list_catalog(a.out)

    from app import careem_mcp, db

    db.init_db()
    target = careem_mcp._target_from_env()
    if target != "production" and not a.allow_sandbox:
        print("❌ CAREEM_TARGET is not production. The CACHED badge promises a REAL production snapshot, "
              "so snapshot.py refuses to run on sandbox data. Set CAREEM_TARGET=production.")
        return 2
    if target == "sandbox" and a.out.resolve() == CATALOG_PATH.resolve():
        print("❌ --allow-sandbox needs --out pointing somewhere other than fixtures/catalog.json")
        return 2
    posts = load_json(a.posts, {}).get("posts") or []
    if a.only:
        posts = [p_ for p_ in posts if p_.get("id") in set(a.only)]
    if not posts:
        print(f"no posts to snapshot (check {a.posts} / --only)")
        return 1
    return snapshot(posts, method=a.method, out=a.out, target=target)


if __name__ == "__main__":
    sys.exit(main())
