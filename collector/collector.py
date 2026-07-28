"""
Modbus TCP polling service.

Reads every tag defined in config.yaml on a fixed interval, applies scaling,
evaluates alarm limits, and writes samples to SQLite. Survives device dropouts
with bounded reconnect backoff and records device online/offline state so the
dashboard can distinguish "no data" from "value is zero".

Usage:
    python collector/collector.py --config config.yaml
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

import yaml
from pymodbus.client import ModbusTcpClient

_HERE = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))
from collector.db import Database, utcnow  # noqa: E402

log = logging.getLogger("collector")

_running = True


def _stop(signum, frame):  # noqa: ARG001
    global _running
    log.info("shutdown requested")
    _running = False


class DeviceReader:
    """Owns one Modbus connection and knows how to read its tag list."""

    def __init__(self, cfg: dict, timeout: float, backoff: float) -> None:
        self.name = cfg["name"]
        self.label = cfg.get("label", cfg["name"])
        self.host = cfg["host"]
        self.port = cfg["port"]
        self.device_id = cfg.get("device_id", 1)
        self.tags = cfg["tags"]
        self.backoff = backoff
        self.client = ModbusTcpClient(self.host, port=self.port, timeout=timeout)
        self._blocked_until = 0.0

    def _connect(self) -> bool:
        if self.client.connected:
            return True
        if time.monotonic() < self._blocked_until:
            return False
        if self.client.connect():
            log.info("%s: connected to %s:%s", self.name, self.host, self.port)
            return True
        self._blocked_until = time.monotonic() + self.backoff
        return False

    def _read_tag(self, tag: dict) -> float | None:
        kind = tag.get("type", "holding")
        address = tag["address"]

        if kind == "discrete":
            rr = self.client.read_discrete_inputs(address, count=1, device_id=self.device_id)
            if rr.isError():
                raise IOError(f"read_discrete_inputs({address}) failed: {rr}")
            return 1.0 if rr.bits[0] else 0.0

        count = 2 if tag.get("width") == 32 else 1
        rr = self.client.read_holding_registers(address, count=count, device_id=self.device_id)
        if rr.isError():
            raise IOError(f"read_holding_registers({address}) failed: {rr}")

        if count == 2:
            raw = rr.registers[0] | (rr.registers[1] << 16)  # low word first
        else:
            raw = rr.registers[0]
            if raw > 32767 and tag.get("signed", True):
                raw -= 65536

        return raw * float(tag.get("scale", 1.0))

    def poll(self) -> tuple[dict[str, float], str | None]:
        """Return (tag values, error). On error the connection is dropped."""
        if not self._connect():
            return {}, f"cannot connect to {self.host}:{self.port}"

        values: dict[str, float] = {}
        try:
            for tag in self.tags:
                value = self._read_tag(tag)
                if value is not None:
                    values[tag["name"]] = value
        except Exception as exc:  # noqa: BLE001 - any protocol error means reconnect
            log.warning("%s: poll failed: %s", self.name, exc)
            self.client.close()
            self._blocked_until = time.monotonic() + self.backoff
            return {}, str(exc)

        return values, None

    def close(self) -> None:
        self.client.close()


def evaluate_alarms(db: Database, device: str, tags: list[dict], values: dict[str, float]) -> None:
    for tag in tags:
        name = tag["name"]
        if name not in values:
            continue
        value = values[name]
        label = tag.get("label", name)
        unit = tag.get("unit", "")

        if tag.get("alarm_on_true"):
            if value >= 0.5:
                db.raise_alarm(device, name, "high", f"{label} aktif")
            else:
                db.clear_alarm(device, name)
            continue

        breach = None
        if "max_warn" in tag and value > tag["max_warn"]:
            breach = f"{label} yüksek: {value:.2f} {unit} (limit {tag['max_warn']} {unit})"
        elif "min_warn" in tag and value < tag["min_warn"]:
            breach = f"{label} düşük: {value:.2f} {unit} (limit {tag['min_warn']} {unit})"

        if breach:
            db.raise_alarm(device, name, "warning", breach)
        else:
            db.clear_alarm(device, name)


def run(config_path: str) -> None:
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    ccfg = cfg.get("collector", {})
    interval = float(ccfg.get("poll_interval_seconds", 2.0))
    timeout = float(ccfg.get("request_timeout_seconds", 3.0))
    backoff = float(ccfg.get("reconnect_backoff_seconds", 5.0))

    db = Database(cfg["database"]["path"])
    purged = db.purge(int(cfg["database"].get("retention_days", 90)))
    if purged:
        log.info("purged %d rows past retention", purged)

    readers = [DeviceReader(d, timeout, backoff) for d in cfg["devices"]]
    tags_by_device = {d["name"]: d["tags"] for d in cfg["devices"]}
    log.info("polling %d device(s) every %.1fs", len(readers), interval)

    last_purge = time.monotonic()

    while _running:
        cycle_start = time.monotonic()

        for reader in readers:
            values, error = reader.poll()
            db.set_device_status(reader.name, online=error is None, error=error)
            if error:
                continue

            ts = utcnow()
            rows = [(ts, reader.name, tag, val, "good") for tag, val in values.items()]
            db.insert_samples(rows)
            evaluate_alarms(db, reader.name, tags_by_device[reader.name], values)

        if time.monotonic() - last_purge > 86400:
            db.purge(int(cfg["database"].get("retention_days", 90)))
            last_purge = time.monotonic()

        elapsed = time.monotonic() - cycle_start
        if elapsed > interval:
            log.warning("poll cycle took %.2fs, longer than interval %.2fs", elapsed, interval)
        time.sleep(max(0.0, interval - elapsed))

    for reader in readers:
        reader.close()
    db.close()
    log.info("collector stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Modbus TCP -> SQLite data collector")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    run(args.config)
