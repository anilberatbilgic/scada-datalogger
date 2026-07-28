"""SQLite storage for time-series tag samples and alarm events."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,
    device     TEXT    NOT NULL,
    tag        TEXT    NOT NULL,
    value      REAL    NOT NULL,
    quality    TEXT    NOT NULL DEFAULT 'good'
);
CREATE INDEX IF NOT EXISTS idx_samples_lookup ON samples (device, tag, ts);

CREATE TABLE IF NOT EXISTS alarms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    raised_at   TEXT    NOT NULL,
    cleared_at  TEXT,
    device      TEXT    NOT NULL,
    tag         TEXT    NOT NULL,
    severity    TEXT    NOT NULL,
    message     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alarms_open ON alarms (device, tag, cleared_at);

CREATE TABLE IF NOT EXISTS device_status (
    device      TEXT PRIMARY KEY,
    online      INTEGER NOT NULL,
    last_seen   TEXT,
    last_error  TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin wrapper around sqlite3. Safe to share across threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------------------------------------------------------- writes

    def insert_samples(self, rows: list[tuple[str, str, str, float, str]]) -> None:
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO samples (ts, device, tag, value, quality) VALUES (?,?,?,?,?)",
                rows,
            )
            self._conn.commit()

    def set_device_status(self, device: str, online: bool, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO device_status (device, online, last_seen, last_error)
                   VALUES (?,?,?,?)
                   ON CONFLICT(device) DO UPDATE SET
                       online=excluded.online,
                       last_seen=excluded.last_seen,
                       last_error=excluded.last_error""",
                (device, int(online), utcnow(), error),
            )
            self._conn.commit()

    def raise_alarm(self, device: str, tag: str, severity: str, message: str) -> None:
        """Open an alarm unless one is already open for this device/tag."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM alarms WHERE device=? AND tag=? AND cleared_at IS NULL",
                (device, tag),
            ).fetchone()
            if existing:
                return
            self._conn.execute(
                "INSERT INTO alarms (raised_at, device, tag, severity, message) VALUES (?,?,?,?,?)",
                (utcnow(), device, tag, severity, message),
            )
            self._conn.commit()

    def clear_alarm(self, device: str, tag: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE alarms SET cleared_at=? WHERE device=? AND tag=? AND cleared_at IS NULL",
                (utcnow(), device, tag),
            )
            self._conn.commit()

    def purge(self, retention_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # ----------------------------------------------------------------- reads

    def latest_values(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            """SELECT s.device, s.tag, s.value, s.ts, s.quality
               FROM samples s
               JOIN (SELECT device, tag, MAX(id) AS max_id
                     FROM samples GROUP BY device, tag) m
                 ON s.id = m.max_id
               ORDER BY s.device, s.tag"""
        ).fetchall()

    def history(self, device: str, tag: str, hours: int = 6, limit: int = 2000) -> list[sqlite3.Row]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self._conn.execute(
            """SELECT ts, value FROM samples
               WHERE device=? AND tag=? AND ts >= ?
               ORDER BY ts DESC LIMIT ?""",
            (device, tag, since, limit),
        ).fetchall()[::-1]

    def open_alarms(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM alarms WHERE cleared_at IS NULL ORDER BY raised_at DESC"
        ).fetchall()

    def recent_alarms(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM alarms ORDER BY raised_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def device_status(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM device_status").fetchall()

    def daily_summary(self, device: str, tag: str, days: int = 7) -> list[sqlite3.Row]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return self._conn.execute(
            """SELECT substr(ts, 1, 10) AS day,
                      MIN(value) AS min_v,
                      AVG(value) AS avg_v,
                      MAX(value) AS max_v,
                      COUNT(*)   AS n
               FROM samples
               WHERE device=? AND tag=? AND ts >= ?
               GROUP BY day ORDER BY day""",
            (device, tag, since),
        ).fetchall()

    def close(self) -> None:
        self._conn.close()
