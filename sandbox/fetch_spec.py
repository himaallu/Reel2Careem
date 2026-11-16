"""Download Careem's public MCP sandbox package into sandbox/spec/ (not redistributed in this repo).

    python -m sandbox.fetch_spec            # only if sandbox/spec is missing
    python -m sandbox.fetch_spec --force    # re-download

Source: https://builders.careem.com/docs/sandbox (Careem's no-code sandbox package, Markdown only).
"""
from __future__ import annotations

import io
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

URL = "https://builders.careem.com/sandbox/careem-mcp-sandbox.zip"
SPEC = Path(__file__).resolve().parent / "spec"


def have_spec() -> bool:
    return (SPEC / "tools").is_dir() and any((SPEC / "tools").rglob("*.md"))


def fetch(force: bool = False) -> bool:
    if have_spec() and not force:
        return True
    print(f"downloading Careem MCP sandbox package from {URL} …")
    try:
        with urllib.request.urlopen(URL, timeout=60) as r:
            data = r.read()
    except OSError as e:
        print(f"❌ download failed: {e}")
        return False
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():                       # refuse path traversal in the archive
            if name.startswith("/") or ".." in Path(name).parts:
                print(f"❌ unsafe path in archive: {name}")
                return False
        z.extractall(tmp)
        src = next(Path(tmp).rglob("spec"), None)
        if src is None or not (src / "tools").is_dir():
            print("❌ archive has no spec/tools folder")
            return False
        if SPEC.exists():
            shutil.rmtree(SPEC)
        shutil.copytree(src, SPEC)
    n = len(list((SPEC / "tools").rglob("*.md")))
    print(f"✅ sandbox spec ready: {n} tool files in {SPEC}")
    return True


if __name__ == "__main__":
    sys.exit(0 if fetch(force="--force" in sys.argv) else 1)
