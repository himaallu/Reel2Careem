"""[P1] Careem Identity: endpoint discovery, PKCE helpers, token refresh.

Public PKCE client (no secret). Tokens are NEVER logged or printed — only lengths/expiry.

    from app.careem_auth import get_access_token
    headers = {"Authorization": f"Bearer {get_access_token()}"}
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = Path(os.getenv("ENV_PATH") or REPO_ROOT / ".env")
load_dotenv(ENV_PATH)

log = logging.getLogger("genie.careem_auth")
DEFAULT_SCOPES = "openid profile offline_access"
HTTP_TIMEOUT = httpx.Timeout(10.0)
EXPIRY_MARGIN_S = 60
DEFAULT_EXPIRES_IN_S = 300  # if the token response omits expires_in


class CareemAuthError(RuntimeError):
    """Auth failure. Message never contains token material."""


# ---------------------------------------------------------------- config
def mcp_url() -> str:
    return os.getenv("CAREEM_MCP_URL", "https://mcp.careemapis.com/mcp")


def client_id() -> str:
    cid = os.getenv("CAREEM_CLIENT_ID", "")
    if not cid:
        raise CareemAuthError("CAREEM_CLIENT_ID is not set")
    return cid


def redirect_uri() -> str:
    return os.getenv("CAREEM_REDIRECT_URI", "http://localhost:8080/callback")


def scopes() -> str:
    return os.getenv("CAREEM_SCOPES", DEFAULT_SCOPES)


def _send_resource() -> bool:
    # RFC 8707 resource indicator (MCP auth spec). Set CAREEM_SEND_RESOURCE=0 if Identity rejects it.
    return os.getenv("CAREEM_SEND_RESOURCE", "1") != "0"


# ---------------------------------------------------------------- discovery
@dataclass(frozen=True)
class Endpoints:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    resource: str
    resource_scopes: tuple[str, ...]


@lru_cache(maxsize=4)
def discover(resource_url: Optional[str] = None) -> Endpoints:
    """MCP protected-resource metadata → authorization server → OpenID configuration."""
    resource_url = resource_url or mcp_url()
    origin = httpx.URL(resource_url).copy_with(path="/", query=None)
    prm_url = str(origin.join("/.well-known/oauth-protected-resource"))
    with httpx.Client(timeout=HTTP_TIMEOUT) as http:
        prm = _get_json(http, prm_url)
        servers = prm.get("authorization_servers") or []
        if not servers:
            raise CareemAuthError(f"no authorization_servers in {prm_url}")
        issuer = servers[0].rstrip("/")
        oidc = _get_json(http, f"{issuer}/.well-known/openid-configuration")
    if "S256" not in (oidc.get("code_challenge_methods_supported") or ["S256"]):
        raise CareemAuthError("authorization server does not support PKCE S256")
    return Endpoints(
        issuer=oidc.get("issuer", issuer),
        authorization_endpoint=oidc["authorization_endpoint"],
        token_endpoint=oidc["token_endpoint"],
        resource=prm.get("resource", resource_url),
        resource_scopes=tuple(prm.get("scopes_supported") or ()),
    )


def _get_json(http: httpx.Client, url: str) -> dict[str, Any]:
    try:
        r = http.get(url)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise CareemAuthError(f"discovery failed for {url}: {type(e).__name__}") from None


# ---------------------------------------------------------------- PKCE / authorize
def pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge) using S256."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(state: str, code_challenge: str) -> str:
    ep = discover()
    params = {
        "response_type": "code",
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "scope": scopes(),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if _send_resource():
        params["resource"] = ep.resource
    return f"{ep.authorization_endpoint}?{urlencode(params)}"


# ---------------------------------------------------------------- token endpoint
def _token_request(form: dict[str, str]) -> dict[str, Any]:
    """POST to the token endpoint as a public client.

    Discovery only advertises client_secret_basic, so if body-auth is rejected as invalid_client
    we retry once with HTTP Basic (client_id, empty secret). A rejected client auth does not
    consume an authorization code, so this retry is safe.
    """
    ep = discover()
    form = {**form, "client_id": client_id()}
    if _send_resource():
        form["resource"] = ep.resource
    with httpx.Client(timeout=HTTP_TIMEOUT) as http:
        try:
            r = http.post(ep.token_endpoint, data=form)
            if (_oauth_error(r) or "").startswith("invalid_client"):
                log.info("token endpoint rejected body client auth; retrying with HTTP Basic")
                r = http.post(ep.token_endpoint, data=form, auth=(client_id(), ""))
        except httpx.HTTPError as e:
            raise CareemAuthError(f"token request failed: {type(e).__name__}") from None
    if r.status_code != 200:
        err = _oauth_error(r) or _describe_non_oauth(r)
        raise CareemAuthError(f"token endpoint HTTP {r.status_code}: {err}")
    try:
        tok = r.json()
    except ValueError:
        raise CareemAuthError("token endpoint returned non-JSON") from None
    if "access_token" not in tok:
        raise CareemAuthError("token response has no access_token")
    return tok


def _oauth_error(r: httpx.Response) -> Optional[str]:
    """Only the error code/description — never echo the body (it may contain tokens)."""
    try:
        body = r.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    desc = body.get("error_description")
    return f"{err}: {desc}" if err and desc else err


def _describe_non_oauth(r: httpx.Response) -> str:
    """Describe a non-OAuth error (e.g. a CDN/WAF block page) without echoing the body."""
    server = r.headers.get("server", "?")
    ctype = r.headers.get("content-type", "?").split(";")[0]
    ray = r.headers.get("cf-ray")
    if ray or server.lower() == "cloudflare":
        title = re.search(r"<title>([^<]{0,120})</title>", r.text or "", re.I)
        return (f"blocked by Cloudflare before reaching Careem Identity "
                f"(cf-ray={ray or '?'}, page title={title.group(1).strip() if title else '?'!r})")
    return f"unknown_error (server={server}, content-type={ctype})"


def exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    return _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "code_verifier": code_verifier,
    })


def refresh(refresh_token: str) -> dict[str, Any]:
    return _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})


# ---------------------------------------------------------------- .env in-place update
def update_env_file(key: str, value: str, path: Path = ENV_PATH) -> None:
    """Set KEY=value in .env, keeping every other line (and comments) as-is. Atomic, chmod 600."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new_line = f"{key}={value}"
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("export "):
            stripped = stripped[len("export "):]
        if stripped.split("=", 1)[0].strip() == key and "=" in stripped:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    path = path.resolve()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.environ[key] = value


def save_refresh_token(token: str) -> None:
    update_env_file("CAREEM_REFRESH_TOKEN", token)


# ---------------------------------------------------------------- access token cache
_lock = threading.Lock()
_access_token: Optional[str] = None
_expires_at: float = 0.0


def _store(tok: dict[str, Any]) -> str:
    global _access_token, _expires_at
    _access_token = tok["access_token"]
    expires_in = int(tok.get("expires_in") or DEFAULT_EXPIRES_IN_S)
    _expires_at = time.monotonic() + max(0, expires_in - EXPIRY_MARGIN_S)
    new_rt = tok.get("refresh_token")
    if new_rt and new_rt != os.getenv("CAREEM_REFRESH_TOKEN"):
        save_refresh_token(new_rt)
        log.info("refresh token rotated and saved to .env")
    log.info("access token refreshed (valid ~%ss)", expires_in)
    return _access_token


def get_access_token(force: bool = False) -> str:
    """Valid bearer token for the Careem MCP. Cached until 60 s before expiry. Thread-safe."""
    with _lock:
        if not force and _access_token and time.monotonic() < _expires_at:
            return _access_token
        rt = os.getenv("CAREEM_REFRESH_TOKEN", "")
        if not rt:
            raise CareemAuthError("CAREEM_REFRESH_TOKEN missing — run scripts/login_careem.py")
        return _store(refresh(rt))


def invalidate() -> None:
    """Drop the cached access token (call on a 401 from the MCP, then get_access_token())."""
    global _access_token, _expires_at
    with _lock:
        _access_token, _expires_at = None, 0.0


def token_status() -> dict[str, Any]:
    """Safe-to-log status (no token material)."""
    return {
        "has_refresh_token": bool(os.getenv("CAREEM_REFRESH_TOKEN")),
        "has_access_token": _access_token is not None,
        "expires_in_s": max(0, int(_expires_at - time.monotonic())) if _access_token else 0,
    }
