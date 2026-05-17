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
    PRIMARY KEY (run_id, spoke_ip),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_run_sites_device ON run_sites(device_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
"""


def init_db(path: Path) -> None:
    global _DB_PATH
    _DB_PATH = path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)


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
    "name", "spoke_ip", "hub_ip", "hub_mgmt_ip", "speed",
    "server_intf", "client_intf", "traffictest_port", "notes",
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
                   (run_id, spoke_ip, device_id, site_status, display_name, hub_ip)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, spoke, device_id, site.get("status"), site.get("display_name", ""), hub),
            )


def get_run(run_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def runs_for_device(device_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT r.id, r.started_at, r.finished_at, r.status, r.exit_code,
                      r.archive_filename, rs.site_status, rs.display_name
               FROM run_sites rs
               JOIN runs r ON r.id = rs.run_id
               WHERE rs.device_id = ?
               ORDER BY r.started_at DESC""",
            (device_id,),
        ).fetchall()
    return [dict(r) for r in rows]
