"""[P2-0] Instagram smoke test: me, media, comments, conversations.

Usage:  python scripts/test_instagram.py
Passes when it prints the Genie account and its media.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ig import IG_API, IG_USER_ID, ME, IGError, get, require_token  # noqa: E402


def show(title: str, data: object) -> None:
    print(f"\n=== {title}")
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main() -> int:
    require_token()
    ok = True

    try:
        fields = "id,username,name,media_count,followers_count"
        if "graph.instagram.com" in IG_API:
            fields += ",user_id,account_type"
        me = get(ME, fields=fields)
        show(f"me ({IG_API})", me)
        real_id = str(me.get("user_id") or me.get("id") or "")
        if not IG_USER_ID:
            print(f"ℹ️  set IG_USER_ID={real_id} in .env")
        elif real_id and real_id != IG_USER_ID:
            print(f"⚠️  IG_USER_ID in .env ({IG_USER_ID}) != account id ({real_id})")
    except IGError as e:
        print(f"❌ me failed: {e}")
        return 1  # nothing else will work

    media: list[dict] = []
    try:
        media = get(f"{ME}/media", fields="id,caption,media_type,media_product_type,permalink,timestamp,username",
                    limit=10).get("data", [])
        show(f"media ({len(media)})", media)
    except IGError as e:
        ok = False
        print(f"❌ media failed: {e}")

    for m in media[:3]:
        try:
            comments = get(f"{m['id']}/comments", fields="id,text,timestamp,username").get("data", [])
            show(f"comments on {m['id']} ({len(comments)})", comments)
        except IGError as e:
            ok = False
            print(f"❌ comments on {m['id']} failed: {e}")

    try:
        convs = get(f"{ME}/conversations", platform="instagram", fields="id,updated_time").get("data", [])
        show(f"conversations ({len(convs)})", convs)
    except IGError as e:
        ok = False
        print(f"❌ conversations failed (needs instagram_business_manage_messages): {e}")

    print("\n✅ all checks passed" if ok else "\n⚠️  some checks failed (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
