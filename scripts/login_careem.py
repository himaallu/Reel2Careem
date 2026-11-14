"""[P1] One-time Careem login on the laptop → CAREEM_REFRESH_TOKEN in .env.

    python scripts/login_careem.py            # opens the browser, catches the callback on :8080
    python scripts/login_careem.py --check    # just refresh with the saved token and report

Never prints tokens.
"""
from __future__ import annotations

import argparse
import logging
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import careem_auth  # noqa: E402

LOGIN_TIMEOUT_S = 300

_PAGE = """<!doctype html><meta charset="utf-8"><title>Careem Genie login</title>
<body style="font-family:system-ui;background:#0b1f17;color:#e6fff3;display:grid;place-items:center;height:100vh">
<div style="text-align:center"><h1>{title}</h1><p>{body}</p></div></body>"""


class _Result:
    code: Optional[str] = None
    error: Optional[str] = None
    done = threading.Event()


def _make_handler(expected_state: str, path: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path != path:
                self.send_error(404)
                return
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if q.get("state") != expected_state:
                _Result.error = "state mismatch (possible stale or forged callback)"
            elif "error" in q:
                _Result.error = f"{q['error']}: {q.get('error_description', '')}".strip(": ")
            elif "code" not in q:
                _Result.error = "callback had no code"
            else:
                _Result.code = q["code"]
            ok = _Result.code is not None
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_PAGE.format(
                title="🧞 Logged in to Careem" if ok else "Login failed",
                body="You can close this tab." if ok else (_Result.error or ""),
            ).encode())
            _Result.done.set()

        def log_message(self, *args: object) -> None:  # keep the code out of stdout
            pass

    return Handler


def login(open_browser: bool = True) -> int:
    ep = careem_auth.discover()
    print(f"Authorization server: {ep.issuer}")
    print(f"Scopes requested:    {careem_auth.scopes()}")
    missing = sorted(set(ep.resource_scopes) - set(careem_auth.scopes().split()))
    if missing:
        print(f"Note: MCP resource also advertises scopes {missing} (set CAREEM_SCOPES to request them)")

    redirect = urlparse(careem_auth.redirect_uri())
    if redirect.hostname not in ("localhost", "127.0.0.1") or not redirect.port:
        print(f"CAREEM_REDIRECT_URI must be a localhost URL with a port, got {redirect.geturl()}")
        return 2

    verifier, challenge = careem_auth.pkce_pair()
    state = secrets.token_urlsafe(24)
    auth_url = careem_auth.build_authorize_url(state, challenge)

    try:
        server = HTTPServer(("127.0.0.1", redirect.port), _make_handler(state, redirect.path or "/"))
    except OSError as e:
        print(f"Cannot listen on port {redirect.port}: {e}. Free the port and retry.")
        return 2
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("\nOpen this URL to log in (opening your browser now):\n")
    print(auth_url + "\n")
    if open_browser:
        webbrowser.open(auth_url)

    got = _Result.done.wait(LOGIN_TIMEOUT_S)
    server.shutdown()
    if not got:
        print(f"Timed out after {LOGIN_TIMEOUT_S}s waiting for the callback.")
        return 1
    if _Result.error:
        print(f"Login failed: {_Result.error}")
        return 1

    try:
        tok = careem_auth.exchange_code(_Result.code or "", verifier)
    except careem_auth.CareemAuthError as e:
        print(f"Code exchange failed: {e}")
        return 1

    rt = tok.get("refresh_token")
    if not rt:
        print("Token response had no refresh_token — is 'offline_access' granted for this client?")
        return 1
    careem_auth.save_refresh_token(rt)
    print(f"✅ CAREEM_REFRESH_TOKEN saved to {careem_auth.ENV_PATH} (length {len(rt)})")
    print(f"   access token expires_in={tok.get('expires_in')}s, granted scope='{tok.get('scope', '?')}'")
    return check()


def check() -> int:
    try:
        careem_auth.get_access_token(force=True)
    except careem_auth.CareemAuthError as e:
        print(f"❌ refresh failed: {e}")
        return 1
    print(f"✅ refresh works: {careem_auth.token_status()}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="only test refresh with the saved token")
    p.add_argument("--no-browser", action="store_true", help="print the URL but don't open a browser")
    a = p.parse_args()
    return check() if a.check else login(open_browser=not a.no_browser)


if __name__ == "__main__":
    sys.exit(main())
