"""
Read-only web dashboard and JSON API over the collected SCADA data.

The dashboard never talks to the PLC. It only reads SQLite, so it cannot
influence the process — which is what makes it safe to expose to office staff.

Usage:
    python web/app.py --config config.yaml --port 8000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request

_HERE = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))
from collector.db import Database  # noqa: E402

app = Flask(__name__)
app.config["CFG"] = {}
app.config["DB"] = None


def cfg() -> dict:
    return app.config["CFG"]


def db() -> Database:
    return app.config["DB"]


def tag_meta() -> dict[tuple[str, str], dict]:
    meta = {}
    for device in cfg()["devices"]:
        for tag in device["tags"]:
            meta[(device["name"], tag["name"])] = tag
    return meta


@app.route("/")
def index():
    devices = [
        {"name": d["name"], "label": d.get("label", d["name"])} for d in cfg()["devices"]
    ]
    return render_template("index.html", devices=devices)


@app.route("/api/snapshot")
def api_snapshot():
    """Everything the dashboard needs for one refresh, in a single request."""
    meta = tag_meta()
    status = {r["device"]: dict(r) for r in db().device_status()}

    values = []
    for row in db().latest_values():
        key = (row["device"], row["tag"])
        t = meta.get(key, {})
        values.append(
            {
                "device": row["device"],
                "tag": row["tag"],
                "label": t.get("label", row["tag"]),
                "unit": t.get("unit", ""),
                "kind": t.get("type", "holding"),
                "value": row["value"],
                "ts": row["ts"],
                "min_warn": t.get("min_warn"),
                "max_warn": t.get("max_warn"),
            }
        )

    return jsonify(
        {
            "devices": [
                {
                    "name": d["name"],
                    "label": d.get("label", d["name"]),
                    "online": bool(status.get(d["name"], {}).get("online", 0)),
                    "last_seen": status.get(d["name"], {}).get("last_seen"),
                    "last_error": status.get(d["name"], {}).get("last_error"),
                }
                for d in cfg()["devices"]
            ],
            "values": values,
            "alarms": [dict(a) for a in db().open_alarms()],
            "alarm_history": [dict(a) for a in db().recent_alarms(20)],
        }
    )


@app.route("/api/history/<device>/<tag>")
def api_history(device: str, tag: str):
    hours = request.args.get("hours", default=6, type=int)
    rows = db().history(device, tag, hours=hours)
    t = tag_meta().get((device, tag), {})
    return jsonify(
        {
            "device": device,
            "tag": tag,
            "label": t.get("label", tag),
            "unit": t.get("unit", ""),
            "points": [{"ts": r["ts"], "value": r["value"]} for r in rows],
        }
    )


@app.route("/api/summary/<device>/<tag>")
def api_summary(device: str, tag: str):
    days = request.args.get("days", default=7, type=int)
    return jsonify([dict(r) for r in db().daily_summary(device, tag, days)])


def create_app(config_path: str) -> Flask:
    conf = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    app.config["CFG"] = conf
    app.config["DB"] = Database(conf["database"]["path"])
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SCADA data logger dashboard")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    create_app(args.config).run(host=args.host, port=args.port, debug=False)
