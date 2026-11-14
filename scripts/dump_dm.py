"""[P2-1] List conversations and dump the latest message(s) with every field we can get,
so we can see exactly how a shared reel arrives.

Usage:
  python scripts/dump_dm.py              # latest message of the most recent conversation
  python scripts/dump_dm.py -c 3 -m 5    # 5 latest messages from each of the 3 newest conversations
  python scripts/dump_dm.py --save       # also write fixtures/dm_share_sample.json (token-free)

Have a tester share a reel to Genie first, then run this.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ig import ME, ROOT, IGError, get, redact, require_token  # noqa: E402

# Richest first. If Meta rejects a field ("nonexisting field"), fall back to the next set.
MESSAGE_FIELD_SETS = [
    "id,created_time,from,to,message,attachments,shares{link,name,description,id},"
    "story,reactions,is_unsupported",
    "id,created_time,from,to,message,attachments,shares,story,is_unsupported",
    "id,created_time,from,to,message,attachments,shares",
    "id,created_time,from,to,message",
]


def list_conversations(limit: int) -> list[dict]:
    data = get(f"{ME}/conversations", platform="instagram",
               fields="id,updated_time,participants", limit=limit)
    return data.get("data", [])


def latest_message_ids(conv_id: str, limit: int) -> list[str]:
    data = get(conv_id, fields=f"messages.limit({limit}){{id,created_time}}")
    return [m["id"] for m in data.get("messages", {}).get("data", [])]


def message_detail(msg_id: str) -> dict:
    last_err = None
    for fields in MESSAGE_FIELD_SETS:
        try:
            msg = get(msg_id, fields=fields)
            msg["_fields_requested"] = fields
            return msg
        except IGError as e:
            last_err = e
    return {"id": msg_id, "_error": str(last_err)}


def summarize(msg: dict) -> None:
    """Point at the places a reel URL / caption / thumbnail could be."""
    hints = []
    for a in (msg.get("attachments") or {}).get("data", []):
        hints.append(f"attachments keys: {sorted(a.keys())}")
    for s in (msg.get("shares") or {}).get("data", []):
        hints.append(f"shares keys: {sorted(s.keys())}")
    if msg.get("is_unsupported"):
        hints.append("is_unsupported=true (API can't render this message type)")
    if not msg.get("message") and not hints:
        hints.append("no text, attachments or shares returned")
    for h in hints:
        print(f"   ↳ {h}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--conversations", type=int, default=1)
    ap.add_argument("-m", "--messages", type=int, default=1)
    ap.add_argument("--save", action="store_true", help="write fixtures/dm_share_sample.json")
    args = ap.parse_args()
    require_token()

    try:
        convs = list_conversations(args.conversations)
    except IGError as e:
        print(f"❌ conversations failed: {e}")
        return 1
    print(f"=== {len(convs)} conversation(s)")
    print(json.dumps(convs, indent=2, ensure_ascii=False))

    dump = []
    for conv in convs:
        try:
            ids = latest_message_ids(conv["id"], args.messages)
        except IGError as e:
            print(f"❌ messages for {conv['id']} failed: {e}")
            continue
        for mid in ids:
            msg = message_detail(mid)
            print(f"\n=== message {mid} (conversation {conv['id']})")
            print(json.dumps(msg, indent=2, ensure_ascii=False))
            summarize(msg)
            dump.append({"conversation": conv, "message": msg})

    if args.save and dump:
        out = ROOT / "fixtures" / "dm_share_sample.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(redact(json.dumps(dump, indent=2, ensure_ascii=False)) + "\n")
        print(f"\nsaved {out.relative_to(ROOT)} (token redacted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
