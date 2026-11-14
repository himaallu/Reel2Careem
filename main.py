"""Starts the web app; the comment poller + tunnel health check start with it (web.py startup)."""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

import uvicorn  # noqa: E402

from app import db  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db.init_db()
    uvicorn.run("app.web:app", host="0.0.0.0", port=int(os.getenv("WEB_PORT", "8000")))


if __name__ == "__main__":
    main()
