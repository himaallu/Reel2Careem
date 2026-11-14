"""[P2-1b] Minimal Instagram webhook receiver to see how a shared reel arrives via webhooks.

Polling /conversations returns shared reels as is_unsupported (see fixtures/dm_share_sample.json),
so this checks whether the `messages` webhook carries the reel (type ig_reel: url, title, reel_video_id).

Usage:
  python scripts/webhook_probe.py            # listens on :8001 (WEBHOOK_PORT)
  cloudflared tunnel --url http://localhost:8001
  Meta dashboard -> Configure webhooks: callback = <tunnel>/webhook, verify token = IG_WEBHOOK_VERIFY_TOKEN

Every POST is printed and saved to fixtures/webhook_samples/<ts>.json. Stdlib only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ig import ROOT, redact  # noqa: E402  (also loads .env)

VERIFY_TOKEN = os.getenv("IG_WEBHOOK_VERIFY_TOKEN", "")
APP_SECRET = os.getenv("IG_APP_SECRET", "")  # optional: enables X-Hub-Signature-256 check
PORT = int(os.getenv("WEBHOOK_PORT", "8001"))
OUT = ROOT / "fixtures" / "webhook_samples"


def signature_ok(body: bytes, header: str | None) -> bool | None:
    """True/False if we can check, None if no app secret is configured."""
    if not APP_SECRET:
        return None
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def describe(payload: dict) -> None:
    """Point at reel data if present."""
    for entry in payload.get("entry", []):
        for ev in entry.get("messaging", []):
            msg = ev.get("message", {})
            print(f"   ↳ from={ev.get('sender', {}).get('id')} mid={msg.get('mid')} text={msg.get('text')!r}")
            for a in msg.get("attachments", []):
                print(f"   ↳ attachment type={a.get('type')} payload keys={sorted((a.get('payload') or {}).keys())}")
            if msg.get("is_unsupported"):
                print("   ↳ is_unsupported=true here too")
        for ch in entry.get("changes", []):
            print(f"   ↳ change field={ch.get('field')}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str = "") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self) -> None:  # Meta verification handshake
        url = urlparse(self.path)
        if url.path != "/webhook":
            return self._send(200, "webhook probe alive")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if q.get("hub.mode") == "subscribe" and VERIFY_TOKEN and q.get("hub.verify_token") == VERIFY_TOKEN:
            print("✅ Meta verification handshake OK")
            return self._send(200, q.get("hub.challenge", ""))
        print("❌ verification failed (wrong verify token?)")
        self._send(403, "forbidden")

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._send(200, "EVENT_RECEIVED")  # ack fast; Meta retries otherwise
        sig = signature_ok(body, self.headers.get("X-Hub-Signature-256"))
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            print(f"non-JSON POST ({len(body)} bytes)")
            return
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 10**6}.json"
        path.write_text(redact(json.dumps(payload, indent=2, ensure_ascii=False)) + "\n")
        print(f"\n=== webhook POST (signature={'n/a' if sig is None else sig}) -> {path.relative_to(ROOT)}")
        print(redact(json.dumps(payload, indent=2, ensure_ascii=False)))
        describe(payload)

    def log_message(self, *_args) -> None:  # keep output to our own prints
        pass


def main() -> int:
    if not VERIFY_TOKEN:
        sys.exit("IG_WEBHOOK_VERIFY_TOKEN is not set in .env")
    print(f"listening on http://localhost:{PORT}/webhook")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
