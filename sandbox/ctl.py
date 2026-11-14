"""Sandbox control CLI (spec/BUILD.md §6).

    python -m sandbox.ctl start | stop | status | logs
    python -m sandbox.ctl scenarios [tool]          # list scenarios
    python -m sandbox.ctl scenario <tool> <name>    # force a scenario (no restart needed)
    python -m sandbox.ctl scenario <tool>           # clear one tool's override
    python -m sandbox.ctl reset                     # clear all overrides
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from sandbox import server as sb

RUN = sb.OVERRIDES_PATH.parent
PID = RUN / "server.pid"
LOG = RUN / "server.log"
PORT = int(os.getenv("SANDBOX_PORT", "8787"))
HEALTH = f"http://127.0.0.1:{PORT}/health"


def _pid() -> int | None:
    try:
        pid = int(PID.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def start() -> int:
    if _pid():
        print(f"already running (pid {_pid()}) at http://127.0.0.1:{PORT}/mcp")
        return 0
    RUN.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    with LOG.open("a") as f:
        p = subprocess.Popen([sys.executable, "-m", "sandbox.server", "--port", str(PORT)],
                             cwd=root, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
    PID.write_text(str(p.pid))
    for _ in range(50):
        if _healthy():
            print(f"started (pid {p.pid}) at http://127.0.0.1:{PORT}/mcp — logs: {LOG}")
            return 0
        if p.poll() is not None:
            break
        time.sleep(0.2)
    print(f"failed to start — see {LOG}")
    return 1


def stop() -> int:
    pid = _pid()
    if not pid:
        print("not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    PID.unlink(missing_ok=True)
    print(f"stopped (pid {pid})")
    return 0


def status() -> int:
    pid = _pid()
    print(f"{'running' if pid else 'stopped'}{f' (pid {pid})' if pid else ''}; healthy={_healthy()}; "
          f"overrides={sb.read_overrides()}")
    return 0 if pid else 1


def logs() -> int:
    print(LOG.read_text()[-5000:] if LOG.exists() else "(no logs)")
    return 0


def _write(over: dict[str, str]) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    sb.OVERRIDES_PATH.write_text(json.dumps(over, indent=2))


def scenarios(tool: str | None) -> int:
    defs = sb.load_all()
    for name in [tool] if tool else sorted(defs):
        if name not in defs:
            print(f"unknown tool {name!r}")
            return 1
        print(f"{name}: " + ", ".join(s.name + ("" if s.match else " (default)") for s in defs[name].scenarios))
    return 0


def scenario(tool: str, name: str | None) -> int:
    defs = sb.load_all()
    if tool not in defs:
        print(f"unknown tool {tool!r}")
        return 1
    over = sb.read_overrides()
    if name is None:
        over.pop(tool, None)
        print(f"cleared override for {tool}")
    else:
        valid = [s.name for s in defs[tool].scenarios]
        if name.lower() not in (v.lower() for v in valid):
            print(f"unknown scenario {name!r}; valid: {', '.join(valid)}")
            return 1
        over[tool] = name
        print(f"{tool} forced to {name!r}")
    _write(over)
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "start":
        return start()
    if cmd == "stop":
        return stop()
    if cmd == "status":
        return status()
    if cmd == "logs":
        return logs()
    if cmd == "reset":
        _write({})
        print("all overrides cleared")
        return 0
    if cmd == "scenarios":
        return scenarios(rest[0] if rest else None)
    if cmd == "scenario" and rest:
        return scenario(rest[0], rest[1] if len(rest) > 1 else None)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
