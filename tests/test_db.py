"""Tests for webapp.db persistence (devices, runs, schedules).

Each test uses the `fresh_db` fixture -> an isolated temp SQLite file.
"""
from __future__ import annotations

import json
from datetime import datetime

from webapp import db


# --- devices -------------------------------------------------------------

def test_create_and_get_device(fresh_db):
    did = db.create_device({
        "name": "spoke-a", "spoke_ip": "10.0.0.1", "hub_ip": "10.0.0.254", "speed": "100M",
    })
    dev = db.get_device(did)
    assert dev is not None
    assert dev["spoke_ip"] == "10.0.0.1"
    assert dev["speed"] == "100M"


def test_update_device(fresh_db, sample_device):
    db.update_device(sample_device, {
        "name": "renamed", "spoke_ip": "10.0.0.1", "hub_ip": "10.0.0.254", "speed": "200M",
    })
    dev = db.get_device(sample_device)
    assert dev["name"] == "renamed"
    assert dev["speed"] == "200M"


def test_delete_device(fresh_db, sample_device):
    db.delete_device(sample_device)
    assert db.get_device(sample_device) is None


def test_find_device_by_spoke_hub(fresh_db, sample_device):
    dev = db.find_device_by_spoke_hub("10.0.0.1", "10.0.0.254")
    assert dev is not None
    assert dev["id"] == sample_device


# --- runs ----------------------------------------------------------------

def test_insert_and_finalize_run(fresh_db):
    started = datetime(2026, 6, 18, 10, 0, 0)
    db.insert_run(
        run_id="20260618-100000", started_at=started,
        source="devices", input_name="2 device(s)", settings={"sshuser": "admin"},
    )
    db.update_run_status("20260618-100000", "running")
    db.finalize_run(
        run_id="20260618-100000", status="done", exit_code=0,
        finished_at=datetime(2026, 6, 18, 10, 5, 0),
        archive_filename="20260618-100000.json",
        summary={"total_sites": 2, "successful_sites": 2, "failed_sites": 0},
    )
    recent = db.recent_runs(limit=6)
    assert len(recent) == 1
    row = recent[0]
    assert row["id"] == "20260618-100000"
    assert row["status"] == "done"
    assert row["summary"]["successful_sites"] == 2


def test_recent_runs_orders_newest_first(fresh_db):
    for i in range(3):
        rid = f"2026061{i}-100000"
        db.insert_run(
            run_id=rid, started_at=datetime(2026, 6, 10 + i, 10, 0, 0),
            source="csv", input_name="x", settings={},
        )
    recent = db.recent_runs(limit=6)
    ids = [r["id"] for r in recent]
    assert ids == sorted(ids, reverse=True)


def test_insert_run_stores_schedule_name(fresh_db):
    db.insert_run(
        run_id="r1", started_at=datetime(2026, 6, 18, 10, 0, 0),
        source="devices", input_name="x", settings={}, schedule_name="nightly",
    )
    # list_runs intentionally omits schedule_name; verify it persisted at rest.
    with db._connect() as conn:
        stored = conn.execute("SELECT schedule_name FROM runs WHERE id = 'r1'").fetchone()
    assert stored["schedule_name"] == "nightly"


# --- schedules -----------------------------------------------------------

def test_create_schedule_and_due(fresh_db):
    sid = db.create_schedule({
        "name": "nightly", "enabled": True, "pattern": "daily",
        "run_time": "02:00", "device_ids": json.dumps([1, 2]),
        "sshuser": "admin", "sshpw": "secret",
        "next_run_at": "2026-06-18T02:00:00",
    })
    assert isinstance(sid, int)

    # Due if now >= next_run_at.
    due = db.due_schedules("2026-06-18T02:00:00")
    assert len(due) == 1
    assert due[0]["name"] == "nightly"

    # Not due if now < next_run_at.
    assert db.due_schedules("2026-06-18T01:59:59") == []


def test_schedule_password_is_encrypted_at_rest(fresh_db):
    """sshpw must not be stored as plaintext."""
    import secret_store

    db.create_schedule({
        "name": "s", "enabled": True, "pattern": "daily", "run_time": "02:00",
        "device_ids": "[]", "sshuser": "admin", "sshpw": "plaintext-pw",
        "next_run_at": "2026-06-18T02:00:00",
    })
    # Read the raw stored value directly.
    with db._connect() as conn:
        raw = conn.execute("SELECT sshpw FROM schedules LIMIT 1").fetchone()["sshpw"]
    assert raw != "plaintext-pw"
    assert secret_store.is_fernet_token(raw)


def test_disabled_schedule_not_due(fresh_db):
    db.create_schedule({
        "name": "off", "enabled": False, "pattern": "daily", "run_time": "02:00",
        "device_ids": "[]", "sshuser": "admin", "sshpw": "x",
        "next_run_at": "2026-06-18T02:00:00",
    })
    assert db.due_schedules("2026-06-18T03:00:00") == []


def test_mark_schedule_fired_updates_status(fresh_db):
    sid = db.create_schedule({
        "name": "s", "enabled": True, "pattern": "daily", "run_time": "02:00",
        "device_ids": "[]", "sshuser": "admin", "sshpw": "x",
        "next_run_at": "2026-06-18T02:00:00",
    })
    db.mark_schedule_fired(
        sid, last_run_at="2026-06-18T02:00:01", last_run_id="run-1",
        last_run_status="fired", last_run_message="started run run-1",
        next_run_at="2026-06-19T02:00:00", enabled=True,
    )
    sched = db.get_schedule(sid)
    assert sched["last_run_status"] == "fired"
    assert sched["last_run_id"] == "run-1"
    assert sched["next_run_at"] == "2026-06-19T02:00:00"
