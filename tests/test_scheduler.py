"""Tests for webapp.scheduler.compute_next_run and the scheduler fire path.

compute_next_run is the heart of scheduling: a regression here means schedules
fire at the wrong time or never. Includes a regression test for the bug where
the startup callable did not accept `schedule_name` and every fire crashed.
"""
from __future__ import annotations

import inspect
from datetime import datetime

from webapp import scheduler


# --- once ----------------------------------------------------------------

def test_once_future_returns_target():
    now = datetime(2026, 6, 18, 10, 0, 0)
    nxt = scheduler.compute_next_run(
        pattern="once", run_time="2026-06-18T12:30", from_time=now,
    )
    assert nxt == datetime(2026, 6, 18, 12, 30)


def test_once_in_past_returns_none():
    now = datetime(2026, 6, 18, 13, 0, 0)
    nxt = scheduler.compute_next_run(
        pattern="once", run_time="2026-06-18T12:30", from_time=now,
    )
    assert nxt is None


def test_once_bad_format_returns_none():
    now = datetime(2026, 6, 18, 10, 0, 0)
    assert scheduler.compute_next_run(pattern="once", run_time="not-a-date", from_time=now) is None


# --- daily ---------------------------------------------------------------

def test_daily_later_today():
    now = datetime(2026, 6, 18, 8, 0, 0)
    nxt = scheduler.compute_next_run(pattern="daily", run_time="09:30", from_time=now)
    assert nxt == datetime(2026, 6, 18, 9, 30)


def test_daily_rolls_to_tomorrow_when_past():
    now = datetime(2026, 6, 18, 10, 0, 0)
    nxt = scheduler.compute_next_run(pattern="daily", run_time="09:30", from_time=now)
    assert nxt == datetime(2026, 6, 19, 9, 30)


def test_daily_exactly_now_rolls_forward():
    now = datetime(2026, 6, 18, 9, 30, 0)
    nxt = scheduler.compute_next_run(pattern="daily", run_time="09:30", from_time=now)
    assert nxt == datetime(2026, 6, 19, 9, 30)


# --- weekly --------------------------------------------------------------

def test_weekly_picks_next_matching_weekday():
    # 2026-06-18 is a Thursday (isoweekday 4). Ask for Monday(1)/Friday(5).
    now = datetime(2026, 6, 18, 12, 0, 0)
    nxt = scheduler.compute_next_run(
        pattern="weekly", run_time="08:00", days_of_week="1,5", from_time=now,
    )
    # Next match is Friday 2026-06-19.
    assert nxt == datetime(2026, 6, 19, 8, 0)
    assert nxt.isoweekday() == 5


def test_weekly_same_day_but_time_passed_rolls_a_week():
    # Thursday(4) requested, but the time already passed today -> next Thursday.
    now = datetime(2026, 6, 18, 12, 0, 0)
    nxt = scheduler.compute_next_run(
        pattern="weekly", run_time="08:00", days_of_week="4", from_time=now,
    )
    assert nxt == datetime(2026, 6, 25, 8, 0)


def test_weekly_no_days_returns_none():
    now = datetime(2026, 6, 18, 12, 0, 0)
    assert scheduler.compute_next_run(
        pattern="weekly", run_time="08:00", days_of_week="", from_time=now,
    ) is None


# --- monthly -------------------------------------------------------------

def test_monthly_this_month():
    now = datetime(2026, 6, 1, 0, 0, 0)
    nxt = scheduler.compute_next_run(
        pattern="monthly", run_time="06:00", day_of_month=15, from_time=now,
    )
    assert nxt == datetime(2026, 6, 15, 6, 0)


def test_monthly_rolls_to_next_month():
    now = datetime(2026, 6, 20, 0, 0, 0)
    nxt = scheduler.compute_next_run(
        pattern="monthly", run_time="06:00", day_of_month=15, from_time=now,
    )
    assert nxt == datetime(2026, 7, 15, 6, 0)


def test_monthly_day_31_clamps_to_short_month():
    # February 2026 has 28 days; day_of_month=31 should clamp to the 28th.
    now = datetime(2026, 2, 1, 0, 0, 0)
    nxt = scheduler.compute_next_run(
        pattern="monthly", run_time="06:00", day_of_month=31, from_time=now,
    )
    assert nxt == datetime(2026, 2, 28, 6, 0)


# --- yearly --------------------------------------------------------------

def test_yearly_this_year():
    now = datetime(2026, 1, 1, 0, 0, 0)
    nxt = scheduler.compute_next_run(
        pattern="yearly", run_time="00:00", day_of_month=25, month_of_year=12, from_time=now,
    )
    assert nxt == datetime(2026, 12, 25, 0, 0)


def test_yearly_rolls_to_next_year():
    now = datetime(2026, 12, 26, 0, 0, 0)
    nxt = scheduler.compute_next_run(
        pattern="yearly", run_time="00:00", day_of_month=25, month_of_year=12, from_time=now,
    )
    assert nxt == datetime(2027, 12, 25, 0, 0)


# --- bad input -----------------------------------------------------------

def test_unknown_pattern_returns_none():
    now = datetime(2026, 6, 18, 10, 0, 0)
    assert scheduler.compute_next_run(pattern="hourly", run_time="09:30", from_time=now) is None


def test_bad_time_string_returns_none():
    now = datetime(2026, 6, 18, 10, 0, 0)
    assert scheduler.compute_next_run(pattern="daily", run_time="9h30", from_time=now) is None


# --- regression: startup callable accepts schedule_name ------------------

def test_fire_one_passes_schedule_name_to_callable(fresh_db):
    """_fire_one always passes schedule_name=; the run callable must accept it.

    Regression for the bug where the FastAPI startup lambda omitted
    schedule_name, so every scheduled fire raised TypeError and nothing ran.
    """
    received = {}

    def fake_start(*, device_ids, sshuser, sshpw, overrides, schedule_name):
        received["schedule_name"] = schedule_name
        received["device_ids"] = device_ids
        return True, "started run abc", "abc"

    schedule = {
        "id": 1,
        "name": "nightly",
        "device_ids": "[1, 2]",
        "overrides_json": "{}",
        "sshuser": "admin",
        "sshpw": "pw",
        "pattern": "once",
        "run_time": "2026-06-18T12:30",
        "enabled": 1,
    }
    # Should not raise; should forward the schedule name through.
    scheduler._fire_one(schedule, start_run_callable=fake_start)
    assert received["schedule_name"] == "nightly"
    assert received["device_ids"] == [1, 2]


def test_app_startup_callable_signature_accepts_schedule_name():
    """The real callable wired in webapp.app must accept schedule_name.

    Guards against re-introducing the original signature-mismatch bug.
    """
    from webapp import app as webapp_app

    sig = inspect.signature(webapp_app._start_run_for_devices)
    assert "schedule_name" in sig.parameters
