#!/usr/bin/env python3
"""
Single entry point.

    python run.py demo        # simulator + collector + dashboard, all at once
    python run.py simulator   # Modbus TCP PLC simulator only
    python run.py collector   # poller only (needs a reachable device)
    python run.py web         # dashboard only (reads the existing database)

`demo` is the fastest way to see the whole pipeline: it starts a simulated
pump station, polls it, and serves the dashboard on http://127.0.0.1:8000.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable


def spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen([PY, *args], cwd=ROOT)


def _on_terminate(signum, frame):  # noqa: ARG001
    """Turn SIGTERM into an exception so the cleanup block still runs.

    Without this, `kill <launcher pid>` leaves the three child processes alive
    and holding their ports.
    """
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description="SCADA data logger launcher")
    parser.add_argument("mode", choices=["demo", "simulator", "collector", "web"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--port", type=int, default=8000, help="dashboard port")
    args = parser.parse_args()

    if args.mode == "simulator":
        return subprocess.call([PY, "simulator/plc_simulator.py"], cwd=ROOT)
    if args.mode == "collector":
        return subprocess.call([PY, "collector/collector.py", "--config", args.config], cwd=ROOT)
    if args.mode == "web":
        return subprocess.call(
            [PY, "web/app.py", "--config", args.config, "--port", str(args.port)], cwd=ROOT
        )

    signal.signal(signal.SIGTERM, _on_terminate)

    names = ["simulator", "collector", "dashboard"]
    procs = [spawn(["simulator/plc_simulator.py"])]
    time.sleep(2)
    procs.append(spawn(["collector/collector.py", "--config", args.config]))
    time.sleep(1)
    procs.append(spawn(["web/app.py", "--config", args.config, "--port", str(args.port)]))

    print(f"\n  Dashboard: http://127.0.0.1:{args.port}   (Ctrl+C ile durdur)\n")
    try:
        while True:
            dead = [(n, p) for n, p in zip(names, procs) if p.poll() is not None]
            if dead:
                name, proc = dead[0]
                print(f"\n  {name} beklenmedik şekilde durdu (exit {proc.returncode}). "
                      f"Diğer süreçler kapatılıyor.\n", file=sys.stderr)
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
