"""[P2] Tiny shared helper for the Instagram smoke scripts. Never prints the token."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

IG_API = os.getenv("IG_API", "https://graph.instagram.com/v23.0").rstrip("/")
IG_TOKEN = os.getenv("IG_TOKEN", "")
IG_USER_ID = os.getenv("IG_USER_ID", "")

_TOKEN_RE = re.compile(r"(access_token=)[^&\s\"']+")


def redact(text: str) -> str:
    """Strip any access token that might appear in a URL or error body."""
    if IG_TOKEN:
        text = text.replace(IG_TOKEN, "***")
    return _TOKEN_RE.sub(r"\1***", text)


def require_token() -> None:
    if not IG_TOKEN:
        sys.exit("IG_TOKEN is not set in .env")


class IGError(Exception):
    pass


def get(path: str, **params: Any) -> dict[str, Any]:
    """GET {IG_API}/{path}. Raises IGError with a redacted message on failure."""
    params["access_token"] = IG_TOKEN
    try:
        r = httpx.get(f"{IG_API}/{path.lstrip('/')}", params=params, timeout=15)
    except httpx.HTTPError as e:  # network error; message may contain the URL
        raise IGError(redact(f"{type(e).__name__}: {e}")) from None
    if r.status_code >= 400:
        raise IGError(redact(f"HTTP {r.status_code}: {r.text}"))
    return r.json()
