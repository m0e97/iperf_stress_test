"""Background poller that fires scheduled runs when their next_run_at is due."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from webapp import db

log = logging.getLogger("scheduler")

POLL_INTERVAL = 30.0  # seconds


def compute_next_run(
    *,
    pattern: str,
    run_time: str,
    days_of_week: str,
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

    # 'HH:MM' for daily/weekly
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
        # Monday=1 .. Sunday=7 in our schema; Python: Monday=0 .. Sunday=6
        try:
            wanted = sorted({int(x) for x in days_of_week.split(",") if x.strip()})
        except ValueError:
            return None
        if not wanted:
            return None
        for offset in range(0, 8):  # search up to 7 days ahead
            day = now + timedelta(days=offset)
            iso_dow = day.isoweekday()  # Monday=1..Sunday=7
            if iso_dow not in wanted:
                continue
            candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
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
            days_of_week=schedule["days_of_week"],
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
