"""
Backfill the database with simulated history so the dashboard has something to
draw before the collector has been running for hours. Demo only — never run
this against a database holding real process data.

Usage:
    python tools/seed_demo_data.py --hours 8 --interval 30
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector.db import Database  # noqa: E402
from simulator.plc_simulator import StationModel  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed demo history")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between samples")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    db = Database(ROOT / cfg["database"]["path"])

    device = cfg["devices"][0]
    tag_by_addr = {(t.get("type", "holding"), t["address"]): t for t in device["tags"]}

    model = StationModel()
    steps = int(args.hours * 3600 / args.interval)
    start = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    rows: list[tuple[str, str, str, float, str]] = []
    for i in range(steps):
        model.step(args.interval)
        ts = (start + timedelta(seconds=i * args.interval)).isoformat(timespec="seconds")

        for addr, raw in enumerate(model.holding_registers()):
            tag = tag_by_addr.get(("holding", addr))
            if not tag:
                continue
            if tag.get("width") == 32:
                raw = raw | (model.holding_registers()[addr + 1] << 16)
            rows.append((ts, device["name"], tag["name"], raw * float(tag.get("scale", 1.0)), "good"))

        for addr, bit in enumerate(model.discrete_inputs()):
            tag = tag_by_addr.get(("discrete", addr))
            if tag:
                rows.append((ts, device["name"], tag["name"], 1.0 if bit else 0.0, "good"))

    db.insert_samples(rows)
    db.set_device_status(device["name"], online=True)
    print(f"seeded {len(rows)} samples over {args.hours} h")


if __name__ == "__main__":
    main()
