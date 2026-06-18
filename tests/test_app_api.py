"""Integration tests for the FastAPI endpoints via TestClient.

Also covers the run-ID format (Riyadh date-time, not random hex) and the
RIYADH_TZ offset, both recently changed.
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from webapp import app as webapp_app
from webapp import db


@pytest.fixture
def client():
    # Point the DB back at the app's own (temp) data dir so endpoints that read
    # runs work regardless of which other test last called init_db.
    db.init_db(webapp_app.DB_PATH)
    return TestClient(webapp_app.app)


# --- health --------------------------------------------------------------

def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "storage" in body
    assert body["storage"]["ok"] is True


# --- /jobs/active --------------------------------------------------------

def test_jobs_active_returns_valid_shape(client):
    r = client.get("/jobs/active")
    assert r.status_code == 200
    body = r.json()
    # With no active job, state is 'empty' or 'idle' depending on history.
    assert body["state"] in {"empty", "idle", "running"}
    assert "history" in body


def test_jobs_active_reports_idle_after_a_finished_run(client):
    rid = "20260618-093000"
    db.insert_run(
        run_id=rid, started_at=datetime(2026, 6, 18, 9, 30, 0),
        source="devices", input_name="1 device(s)", settings={},
    )
    db.finalize_run(
        run_id=rid, status="done", exit_code=0,
        finished_at=datetime(2026, 6, 18, 9, 35, 0),
        archive_filename=None,
        summary={"total_sites": 1, "successful_sites": 1, "failed_sites": 0},
    )
    body = client.get("/jobs/active").json()
    assert body["state"] == "idle"
    assert body["status"] == "done"


# --- run-id format (Riyadh date-time) ------------------------------------

def test_new_job_id_is_riyadh_datetime_format():
    """Regression: run names must be 'YYYYMMDD-HHMMSS', not random hex."""
    job = webapp_app._new_job(source="csv", input_name="test.csv")
    try:
        assert re.fullmatch(r"\d{8}-\d{6}", job.id), f"unexpected id: {job.id}"
    finally:
        # Clean up global active-job state so we don't block later logic.
        with webapp_app.JOBS_LOCK:
            webapp_app.JOBS.pop(job.id, None)
            webapp_app.ACTIVE_JOB_ID = None


def test_riyadh_timezone_is_gmt_plus_3():
    offset = webapp_app.RIYADH_TZ.utcoffset(None)
    assert offset.total_seconds() == 3 * 3600
