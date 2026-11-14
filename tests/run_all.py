"""Run every offline test suite. Starts the local sandbox if it is not running. Exit 1 on any failure.

    python -m tests.run_all
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request

SUITES = ["sandbox.test_acceptance", "tests.test_agent", "tests.test_fallbacks", "tests.test_snapshot",
          "tests.test_orders", "tests.test_intent", "tests.test_consistency"]


def sandbox_up() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8787/health", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def main() -> int:
    if not sandbox_up():
        subprocess.run([sys.executable, "-m", "sandbox.ctl", "start"], check=False)
    if not sandbox_up():
        print("❌ sandbox is not running and could not be started (python -m sandbox.ctl logs)")
        return 1
    failed = []
    for s in SUITES:
        r = subprocess.run([sys.executable, "-m", s], capture_output=True, text=True)
        last = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
        ok = r.returncode == 0
        print(f"{'✅' if ok else '❌'} {s:26} {last}")
        if not ok:
            failed.append(s)
            print("\n".join("     " + ln for ln in (r.stdout + r.stderr).splitlines() if "FAIL" in ln or "Error" in ln)[-2000:])
    print(f"\n{'ALL SUITES PASS' if not failed else f'{len(failed)} suite(s) failed: {failed}'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
