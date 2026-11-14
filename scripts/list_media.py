"""[P2-2] List Genie's media (incl. collab posts) and fill IG_WATCH_MEDIA_IDS in .env.

Usage:
  python scripts/list_media.py               # list media + comment counts
  python scripts/list_media.py --comments    # also print the latest comments per post
  python scripts/list_media.py --write       # write all listed IDs to IG_WATCH_MEDIA_IDS
  python scripts/list_media.py --write --collab-only   # only posts owned by another account

A collab post is owned by the creator/restaurant account; Genie is the accepted collaborator.
Those show up with username != Genie's username (marked COLLAB).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ig import ME, ROOT, IGError, get, require_token  # noqa: E402

MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,timestamp,username,comments_count"


def genie_username() -> str:
    return get(ME, fields="username").get("username", "")


def list_media(limit: int) -> list[dict]:
    return get(f"{ME}/media", fields=MEDIA_FIELDS, limit=limit).get("data", [])


def latest_comments(media_id: str, n: int = 5) -> list[dict]:
    return get(f"{media_id}/comments", fields="id,text,timestamp,username", limit=n).get("data", [])


def write_env(ids: list[str]) -> None:
    env = ROOT / ".env"
    text = env.read_text()
    line = f"IG_WATCH_MEDIA_IDS={','.join(ids)}"
    if re.search(r"^IG_WATCH_MEDIA_IDS=.*$", text, flags=re.M):
        text = re.sub(r"^IG_WATCH_MEDIA_IDS=.*$", line, text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    env.write_text(text)
    print(f"\nwrote {line} to .env")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--comments", action="store_true", help="print latest comments per post")
    ap.add_argument("--write", action="store_true", help="fill IG_WATCH_MEDIA_IDS in .env")
    ap.add_argument("--collab-only", action="store_true", help="only posts owned by another account")
    args = ap.parse_args()
    require_token()

    try:
        me = genie_username()
        media = list_media(args.limit)
    except IGError as e:
        print(f"❌ {e}")
        return 1

    print(f"Genie = @{me}; {len(media)} media item(s)\n")
    selected = []
    for m in media:
        collab = bool(m.get("username")) and m["username"] != me
        if args.collab_only and not collab:
            continue
        selected.append(m["id"])
        caption = (m.get("caption") or "").replace("\n", " ")[:80]
        tag = "COLLAB" if collab else "OWN   "
        print(f"{tag} {m['id']}  @{m.get('username')}  {m.get('media_product_type') or m.get('media_type')}  "
              f"comments={m.get('comments_count', '?')}  {m.get('timestamp', '')}")
        print(f"       {m.get('permalink', '')}")
        print(f"       {caption!r}")
        if args.comments:
            try:
                for c in latest_comments(m["id"]):
                    print(f"         💬 @{c.get('username')}: {c.get('text')!r}  ({c['id']})")
            except IGError as e:
                print(f"         ❌ comments: {e}")

    if not media:
        print("No media visible to Genie's token yet. Create/accept the collab posts first.")
    if args.write:
        if selected:
            write_env(selected)
        else:
            print("\nnothing selected; IG_WATCH_MEDIA_IDS left unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
