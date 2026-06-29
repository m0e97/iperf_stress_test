"""App-wide clock.

Every user-facing timestamp in the project — run ids, run started/finished
times, report "Generated at"/per-site times, schedule next-run/last-run, and DB
created_at values — goes through this module so they all agree with each other
and with the UI clock.

The timezone defaults to GMT+3 (Riyadh) and is configurable via the
``IPERF_TZ_OFFSET`` environment variable, expressed in hours (e.g. ``0`` for
UTC, ``5.5`` for IST, ``-4`` for EDT). Without this, a server running in UTC
shows timestamps 3 hours behind the Riyadh clock shown in the UI.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

DEFAULT_TZ_OFFSET_HOURS = 3.0  # Riyadh / GMT+3


def app_timezone() -> timezone:
    """The configured application timezone as a fixed-offset tzinfo."""
    raw = os.environ.get("IPERF_TZ_OFFSET", "")
    try:
        offset = float(raw) if raw.strip() != "" else DEFAULT_TZ_OFFSET_HOURS
    except ValueError:
        offset = DEFAULT_TZ_OFFSET_HOURS
    return timezone(timedelta(hours=offset))


def now() -> datetime:
    """Current wall-clock time in the app timezone, as a naive ``datetime``.

    Returning a naive value (no tzinfo) keeps stored/displayed ISO strings
    offset-free (e.g. ``2026-06-24T15:13:00``), matching the existing display
    code that slices the ISO string, while still reflecting the app timezone.
    """
    return datetime.now(app_timezone()).replace(tzinfo=None)
