from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

_DB_LOCK = threading.RLock()
_DB_PATH: Path | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT '',
    spoke_ip TEXT NOT NULL,
    hub_ip TEXT NOT NULL,
    hub_mgmt_ip TEXT DEFAULT '',
    speed TEXT DEFAULT '',
    server_intf TEXT DEFAULT '',
    client_intf TEXT DEFAULT '',
    traffictest_port TEXT DEFAULT '',
    circuit_id TEXT DEFAULT '',
    isp TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(spoke_ip, hub_ip)
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    exit_code INTEGER,
    source TEXT NOT NULL,            -- 'csv' | 'devices'
    input_name TEXT DEFAULT '',
    archive_filename TEXT,
    summary_json TEXT,
    settings_json TEXT
);

CREATE TABLE IF NOT EXISTS run_sites (
    run_id TEXT NOT NULL,
    spoke_ip TEXT NOT NULL,
    device_id INTEGER,
    site_status TEXT,
    display_name TEXT DEFAULT '',
    hub_ip TEXT DEFAULT '',
    throughput_mbps REAL,
    PRIMARY KEY (run_id, spoke_ip),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_run_sites_device ON run_sites(device_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    pattern TEXT NOT NULL,                  -- 'once' | 'daily' | 'weekly'
    run_time TEXT NOT NULL,                 -- 'HH:MM' for daily/weekly, 'YYYY-MM-DDTHH:MM' for once
    days_of_week TEXT DEFAULT '',           -- '1,3,5' (mon=1..sun=7) for weekly
    device_ids TEXT NOT NULL,               -- JSON array of device ids
    sshuser TEXT NOT NULL,
    sshpw TEXT NOT NULL,                    -- plaintext; protect the DB volume
    overrides_json TEXT NOT NULL DEFAULT '{}',
    next_run_at TEXT,                       -- ISO timestamp of next scheduled fire
    last_run_at TEXT,
    last_run_id TEXT,
    last_run_status TEXT,                   -- 'fired' | 'skipped_busy' | 'error'
    last_run_message TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_next ON schedules(enabled, next_run_at);
"""


def init_db(path: Path) -> None:
    global _DB_PATH
    _DB_PATH = path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column additions."""
    rs_cols = {row["name"] for row in conn.execute("PRAGMA table_info(run_sites)")}
    if "throughput_mbps" not in rs_cols:
        conn.execute("ALTER TABLE run_sites ADD COLUMN throughput_mbps REAL")

    sched_cols = {row["name"] for row in conn.execute("PRAGMA table_info(schedules)")}
    if "day_of_month" not in sched_cols:
        conn.execute("ALTER TABLE schedules ADD COLUMN day_of_month INTEGER")
    if "month_of_year" not in sched_cols:
        conn.execute("ALTER TABLE schedules ADD COLUMN month_of_year INTEGER")

    dev_cols = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
    if "circuit_id" not in dev_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN circuit_id TEXT DEFAULT ''")
    if "isp" not in dev_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN isp TEXT DEFAULT ''")
    if "accepted_speed" not in dev_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN accepted_speed TEXT DEFAULT ''")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    if _DB_PATH is None:
        raise RuntimeError("DB not initialized. Call init_db() first.")
    with _DB_LOCK:
        conn = sqlite3.connect(_DB_PATH, isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()


# --- Devices --------------------------------------------------------------

DEVICE_COLUMNS = (
    "name", "spoke_ip", "hub_ip", "hub_mgmt_ip", "speed", "accepted_speed",
    "server_intf", "client_intf", "traffictest_port",
    "circuit_id", "isp", "notes",
)


def list_devices() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.*,
                   (SELECT COUNT(*) FROM run_sites rs WHERE rs.device_id = d.id) AS run_count,
                   (SELECT MAX(r.started_at) FROM run_sites rs
                      JOIN runs r ON r.id = rs.run_id WHERE rs.device_id = d.id) AS last_run
            FROM devices d
            ORDER BY d.spoke_ip
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_device(device_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return dict(row) if row else None


def find_device_by_spoke_hub(spoke_ip: str, hub_ip: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE spoke_ip = ? AND hub_ip = ?",
            (spoke_ip, hub_ip),
        ).fetchone()
    return dict(row) if row else None


def create_device(values: dict[str, str]) -> int:
    payload = {col: (values.get(col) or "").strip() for col in DEVICE_COLUMNS}
    payload["created_at"] = datetime.now().isoformat(timespec="seconds")
    cols = list(payload.keys())
    placeholders = ",".join("?" for _ in cols)
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO devices ({','.join(cols)}) VALUES ({placeholders})",
            [payload[c] for c in cols],
        )
        return int(cur.lastrowid)


def update_device(device_id: int, values: dict[str, str]) -> None:
    payload = {col: (values.get(col) or "").strip() for col in DEVICE_COLUMNS}
    assignments = ",".join(f"{col} = ?" for col in payload)
    with _connect() as conn:
        conn.execute(
            f"UPDATE devices SET {assignments} WHERE id = ?",
            [*payload.values(), device_id],
        )


def delete_device(device_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))


def upsert_devices_from_rows(rows: list[dict[str, str]]) -> tuple[int, int]:
    """Bulk-insert from CSV/XLSX-style rows. Returns (inserted, updated)."""
    inserted = updated = 0
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        for row in rows:
            spoke = (row.get("spoke_ip") or "").strip()
            hub = (row.get("hub_ip") or "").strip()
            if not spoke or not hub:
                continue
            existing = conn.execute(
                "SELECT id FROM devices WHERE spoke_ip = ? AND hub_ip = ?",
                (spoke, hub),
            ).fetchone()
            payload = {col: (row.get(col) or "").strip() for col in DEVICE_COLUMNS}
            payload["spoke_ip"] = spoke
            payload["hub_ip"] = hub
            if existing:
                assignments = ",".join(f"{col} = ?" for col in payload)
                conn.execute(
                    f"UPDATE devices SET {assignments} WHERE id = ?",
                    [*payload.values(), existing["id"]],
                )
                updated += 1
            else:
                payload["created_at"] = now
                cols = list(payload.keys())
                placeholders = ",".join("?" for _ in cols)
                conn.execute(
                    f"INSERT INTO devices ({','.join(cols)}) VALUES ({placeholders})",
                    [payload[c] for c in cols],
                )
                inserted += 1
    return inserted, updated


# --- Runs -----------------------------------------------------------------

def insert_run(
    *,
    run_id: str,
    started_at: datetime,
    source: str,
    input_name: str,
    settings: dict[str, Any],
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO runs (id, started_at, status, source, input_name, settings_json)
               VALUES (?, ?, 'pending', ?, ?, ?)""",
            (run_id, started_at.isoformat(timespec="seconds"), source, input_name, json.dumps(settings)),
        )


def update_run_status(run_id: str, status: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))


def finalize_run(
    *,
    run_id: str,
    status: str,
    exit_code: int | None,
    finished_at: datetime,
    archive_filename: str | None,
    summary: dict[str, Any],
) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE runs SET status = ?, exit_code = ?, finished_at = ?,
                              archive_filename = ?, summary_json = ?
               WHERE id = ?""",
            (status, exit_code, finished_at.isoformat(timespec="seconds"),
             archive_filename, json.dumps(summary), run_id),
        )


def insert_run_sites(run_id: str, sites: list[dict[str, Any]]) -> None:
    with _connect() as conn:
        for site in sites:
            spoke = site.get("spoke_ip") or ""
            hub = site.get("hub_ip") or ""
            device_id = site.get("device_id")
            if device_id is None and spoke and hub:
                matched = conn.execute(
                    "SELECT id FROM devices WHERE spoke_ip = ? AND hub_ip = ?",
                    (spoke, hub),
                ).fetchone()
                if matched:
                    device_id = matched["id"]
            conn.execute(
                """INSERT OR REPLACE INTO run_sites
                   (run_id, spoke_ip, device_id, site_status, display_name, hub_ip, throughput_mbps)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, spoke, device_id, site.get("status"), site.get("display_name", ""),
                 hub, site.get("throughput_mbps")),
            )


def get_run(run_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


# --- Schedules ------------------------------------------------------------

def list_schedules() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM schedules ORDER BY enabled DESC, name").fetchall()
    return [dict(r) for r in rows]


def get_schedule(schedule_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return dict(row) if row else None


def due_schedules(now_iso: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ? ORDER BY next_run_at",
            (now_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def _schedule_payload(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": (values.get("name") or "").strip(),
        "enabled": 1 if values.get("enabled", True) else 0,
        "pattern": (values.get("pattern") or "").strip(),
        "run_time": (values.get("run_time") or "").strip(),
        "days_of_week": (values.get("days_of_week") or "").strip(),
        "day_of_month": values.get("day_of_month"),
        "month_of_year": values.get("month_of_year"),
        "device_ids": values.get("device_ids") or "[]",
        "sshuser": (values.get("sshuser") or "").strip(),
        "sshpw": values.get("sshpw") or "",
        "overrides_json": values.get("overrides_json") or "{}",
        "next_run_at": values.get("next_run_at"),
    }


def create_schedule(values: dict[str, Any]) -> int:
    payload = _schedule_payload(values)
    payload["created_at"] = datetime.now().isoformat(timespec="seconds")
    cols = list(payload.keys())
    placeholders = ",".join("?" for _ in cols)
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO schedules ({','.join(cols)}) VALUES ({placeholders})",
            [payload[c] for c in cols],
        )
        return int(cur.lastrowid)


def update_schedule(schedule_id: int, values: dict[str, Any]) -> None:
    payload = _schedule_payload(values)
    assignments = ",".join(f"{col} = ?" for col in payload)
    with _connect() as conn:
        conn.execute(
            f"UPDATE schedules SET {assignments} WHERE id = ?",
            [*payload.values(), schedule_id],
        )


def delete_schedule(schedule_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


def set_schedule_enabled(schedule_id: int, enabled: bool, next_run_at: str | None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE schedules SET enabled = ?, next_run_at = ? WHERE id = ?",
            (1 if enabled else 0, next_run_at, schedule_id),
        )


def mark_schedule_fired(
    schedule_id: int,
    *,
    last_run_at: str,
    last_run_id: str | None,
    last_run_status: str,
    last_run_message: str,
    next_run_at: str | None,
    enabled: bool,
) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE schedules
               SET last_run_at = ?, last_run_id = ?, last_run_status = ?, last_run_message = ?,
                   next_run_at = ?, enabled = ?
               WHERE id = ?""",
            (last_run_at, last_run_id, last_run_status, last_run_message,
             next_run_at, 1 if enabled else 0, schedule_id),
        )


def runs_for_device(device_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT r.id, r.started_at, r.finished_at, r.status, r.exit_code,
                      r.archive_filename, rs.site_status, rs.display_name,
                      rs.throughput_mbps
               FROM run_sites rs
               JOIN runs r ON r.id = rs.run_id
               WHERE rs.device_id = ?
               ORDER BY r.started_at DESC""",
            (device_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def dashboard_stats() -> dict[str, Any]:
    """Aggregate stats for the home dashboard."""
    with _connect() as conn:
        total_devices = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        total_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        active_schedules = conn.execute("SELECT COUNT(*) FROM schedules WHERE enabled=1").fetchone()[0]
        total_schedules = conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]

        recent_run_rows = conn.execute(
            """SELECT id, started_at, finished_at, status, exit_code, source, input_name, summary_json
               FROM runs ORDER BY started_at DESC LIMIT 8"""
        ).fetchall()

        # Most recent throughput per device for health computation
        device_health_rows = conn.execute(
            """SELECT d.id, d.speed, d.accepted_speed,
                      (SELECT rs.throughput_mbps
                       FROM run_sites rs JOIN runs r ON r.id = rs.run_id
                       WHERE rs.device_id = d.id
                       ORDER BY r.started_at DESC LIMIT 1) AS last_throughput
               FROM devices d"""
        ).fetchall()

    recent_runs = []
    for row in recent_run_rows:
        r = dict(row)
        try:
            r["summary"] = json.loads(r["summary_json"]) if r.get("summary_json") else {}
        except Exception:
            r["summary"] = {}
        recent_runs.append(r)

    return {
        "total_devices": total_devices,
        "total_runs": total_runs,
        "active_schedules": active_schedules,
        "total_schedules": total_schedules,
        "recent_runs": recent_runs,
        "device_health": [dict(r) for r in device_health_rows],
    }
