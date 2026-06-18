"""Shared pytest fixtures.

IMPORTANT: ``IPERF_DATA_DIR`` is set at import time (before any ``webapp`` module
is imported) so the app and DB never touch the real ``data/`` directory during
tests. ``webapp.app`` calls ``db.init_db(DATA_DIR/'app.db')`` at import time, so
this must happen first.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# --- Redirect all app data to a throwaway temp dir BEFORE importing webapp. ---
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="iperf_tests_"))
os.environ["IPERF_DATA_DIR"] = str(_TEST_DATA_DIR)
atexit.register(lambda: shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True))

# Make the repo root importable (so `import main`, `import webapp...` work).
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from webapp import db  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    """Initialize a brand-new SQLite DB for a single test and return its path.

    Each test gets an isolated database file so tests never share state.
    """
    db_path = tmp_path / "test_app.db"
    db.init_db(db_path)
    return db_path


@pytest.fixture
def sample_device(fresh_db):
    """Create one device and return its id."""
    return db.create_device({
        "name": "spoke-a",
        "spoke_ip": "10.0.0.1",
        "hub_ip": "10.0.0.254",
        "speed": "100M",
    })
