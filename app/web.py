"""[P2] FastAPI routes: /c/{token}, /api/*, /dashboard, /api/stream (SSE), /simulate. TODO P2-5..P2-7."""
from __future__ import annotations

from fastapi import FastAPI

from app import db

app = FastAPI(title="Careem Genie")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "components": db.get_health()}
