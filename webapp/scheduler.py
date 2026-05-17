"""Background poller that fires scheduled runs when their next_run_at is due."""
from __future__ import annotations

import calendar
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

from webapp import db

log = logging.getLogger("scheduler")

POLL_INTERVAL = 30.0  # seconds


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def compute_next_run(
    *,
    pattern: str,
    run_time: str,
    days_of_week: str = "",
    day_of_month: int | None = None,
    month_of_year: int | None = None,
    from_time: datetime | None = None,
) -> datetime | None:
    """Return the next fire time for a schedule, or None if the schedule is exhausted."""
    now = from_time or datetime.now()

    if pattern == "once":
        try:
            target = datetime.fromisoformat(run_time)
        except ValueError:
            return None
        return target if target > now else None

    # All recurring patterns expect 'HH:MM'.
    try:
        hour_s, minute_s = run_time.split(":", 1)
        hour, minute = int(hour_s), int(minute_s)
    except ValueError:
        return None

    if pattern == "daily":
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if pattern == "weekly":
        try:
            wanted = sorted({int(x) for x in days_of_week.split(",") if x.strip()})
        except ValueError:
            return None
        if not wanted:
            return None
        for offset in range(0, 8):
            day = now + timedelta(days=offset)
            if day.isoweekday() not in wanted:
                continue
            candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > now:
                return candidate
        return None

    if pattern == "monthly":
        if not day_of_month or day_of_month < 1 or day_of_month > 31:
            return None
        y, m = now.year, now.month
        for _ in range(13):
            clamped = min(day_of_month, _last_day(y, m))
            candidate = datetime(y, m, clamped, hour, minute)
            if candidate > now:
                return candidate
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return None

    if pattern == "yearly":
        if not month_of_year or month_of_year < 1 or month_of_year > 12:
            return None
        if not day_of_month or day_of_month < 1 or day_of_month > 31:
            return None
        for year_offset in range(0, 2):
            y = now.year + year_offset
            clamped = min(day_of_month, _last_day(y, month_of_year))
            candidate = datetime(y, month_of_year, clamped, hour, minute)
            if candidate > now:
                return candidate
        return None

    return None


def _fire_one(
    schedule: dict,
    *,
    start_run_callable: Callable[..., tuple[bool, str, str | None]],
) -> None:
    """Attempt to fire one schedule. Updates DB with outcome and next_run_at."""
    sid = schedule["id"]
    name = schedule["name"]
    now = datetime.now()

    try:
        device_ids = [int(x) for x in json.loads(schedule["device_ids"]) if str(x).isdigit() or isinstance(x, int)]
        overrides = json.loads(schedule["overrides_json"] or "{}")
    except (ValueError, TypeError) as exc:
        log.warning("Schedule %s (%s) has invalid payload: %s", sid, name, exc)
        db.mark_schedule_fired(
            sid, last_run_at=now.isoformat(timespec="seconds"),
            last_run_id=None, last_run_status="error",
            last_run_message=f"Invalid payload: {exc}",
            next_run_at=None, enabled=False,
        )
        return

    ok, message, run_id = start_run_callable(
        device_ids=device_ids,
        sshuser=schedule["sshuser"],
        sshpw=schedule["sshpw"],
        overrides=overrides,
    )

    if ok:
        status = "fired"
        log.info("Schedule %s (%s) fired -> run %s", sid, name, run_id)
    else:
        status = "skipped_busy" if "in progress" in message.lower() else "error"
        log.info("Schedule %s (%s) not fired: %s", sid, name, message)

    # Recompute next_run_at; 'once' schedules are disabled after fire/skip.
    if schedule["pattern"] == "once":
        next_at = None
        enabled = False
    else:
        next_dt = compute_next_run(
            pattern=schedule["pattern"],
            run_time=schedule["run_time"],
            days_of_week=schedule.get("days_of_week", ""),
            day_of_month=schedule.get("day_of_month"),
            month_of_year=schedule.get("month_of_year"),
            from_time=now + timedelta(seconds=1),
        )
        next_at = next_dt.isoformat(timespec="seconds") if next_dt else None
        enabled = bool(schedule["enabled"])

    db.mark_schedule_fired(
        sid,
        last_run_at=now.isoformat(timespec="seconds"),
        last_run_id=run_id,
        last_run_status=status,
        last_run_message=message,
        next_run_at=next_at,
        enabled=enabled,
    )


def start(start_run_callable: Callable[..., tuple[bool, str, str | None]]) -> threading.Thread:
    """Start the poller thread. Returns the thread for joinability/tests."""
    stop_event = threading.Event()

    def _loop() -> None:
        log.info("Scheduler poller started (interval=%.0fs)", POLL_INTERVAL)
        while not stop_event.is_set():
            try:
                now_iso = datetime.now().isoformat(timespec="seconds")
                due = db.due_schedules(now_iso)
                for s in due:
                    _fire_one(s, start_run_callable=start_run_callable)
            except Exception:  # noqa: BLE001
                log.exception("Scheduler poll iteration failed")
            stop_event.wait(POLL_INTERVAL)

    t = threading.Thread(target=_loop, name="scheduler", daemon=True)
    t.start()
    return t
