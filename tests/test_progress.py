"""Tests for JobState progress parsing (the run-bar / console progress link).

The engine prints lines like "[3/12] Running site 'x'". JobState parses these
into current_step/total_steps which the run-bar and /jobs/active expose. The
percentage shown in the bar is current/total, so it must reflect the console.
"""
from __future__ import annotations

from webapp.app import JobState


def _job() -> JobState:
    return JobState(id="test")


def test_parses_basic_progress_marker():
    job = _job()
    job.append_line("[3/12] Running site 'riyadh-1'")
    assert job.current_step == 3
    assert job.total_steps == 12
    assert "Running site" in job.current_message


def test_progress_advances_with_each_marker():
    job = _job()
    for i in range(1, 6):
        job.append_line(f"[{i}/5] Running site 'site-{i}'")
        assert job.current_step == i
        assert job.total_steps == 5


def test_non_progress_lines_do_not_change_progress():
    job = _job()
    job.append_line("[2/4] Running site 'x'")
    job.append_line("  some detail output")
    job.append_line("STDOUT: blah")
    assert job.current_step == 2
    assert job.total_steps == 4


def test_strips_trailing_carriage_return():
    """Regression: Windows print emits '\\r\\n'; after splitting on '\\n' the
    trailing '\\r' broke the `$`-anchored progress regex and the bar froze."""
    job = _job()
    job.append_line("[7/9] Running site 'crlf'\r")
    assert job.current_step == 7
    assert job.total_steps == 9
    # Stored log line should not keep the stray CR.
    assert job.log_lines[-1] == "[7/9] Running site 'crlf'"


def test_percentage_matches_console_step():
    """The fill width is round(current/total*100). Verify the ratio the
    frontend uses stays consistent with the parsed step."""
    job = _job()
    job.append_line("[1/4] Running site 'a'")
    assert round(job.current_step / job.total_steps * 100) == 25
    job.append_line("[2/4] Running site 'b'")
    assert round(job.current_step / job.total_steps * 100) == 50
    job.append_line("[4/4] Running site 'd'")
    assert round(job.current_step / job.total_steps * 100) == 100


def test_indented_hub_marker_does_not_hijack_progress():
    """Hub lines look like '  [Hub x] [2/5] ...' and are indented, so the
    top-level [n/m] regex (anchored with match) must NOT pick them up."""
    job = _job()
    job.append_line("[1/3] Running site 'a'")
    job.append_line("  [Hub 10.0.0.1] [2/5] Running spoke")
    # The indented hub marker should be ignored; site progress stays at 1/3.
    assert (job.current_step, job.total_steps) == (1, 3)


def test_parallel_mode_global_markers_drive_progress():
    """Regression: in parallel hub-queue mode the engine emits indented per-hub
    lines plus a column-0 '[done/total] Completed ...' marker per spoke. Only the
    global markers should advance the bar, and it must reach total."""
    job = _job()
    stream = [
        "Running spoke tests across 1 hub queue(s) in parallel...",
        "  [Hub 10.0.0.1] [1/2] Discovering name (10.0.0.2)",
        "  [Hub 10.0.0.1] [1/2] Running spoke '10.0.0.2' (10.0.0.2)",
        "[1/2] Completed '10.0.0.2' (10.0.0.2)",
        "  [Hub 10.0.0.1] [2/2] Running spoke '10.0.0.3' (10.0.0.3)",
        "[2/2] Completed '10.0.0.3' (10.0.0.3)",
    ]
    for ln in stream:
        job.append_line(ln)
    assert (job.current_step, job.total_steps) == (2, 2)
    assert "Completed" in job.current_message
