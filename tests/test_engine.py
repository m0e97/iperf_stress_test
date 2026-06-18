"""Tests for pure engine helpers in main.py.

These functions drive the numbers in every report (speed parsing, pass/fail
classification, summary counts). A regression here silently mislabels results.
"""
from __future__ import annotations

from datetime import datetime

import main as engine
from main import CommandResult, SiteDefinition, SiteRun


# --- parse_speed_to_mbps -------------------------------------------------

def test_parse_speed_plain_number_is_mbps():
    assert engine.parse_speed_to_mbps("100") == 100.0


def test_parse_speed_mbps_unit():
    assert engine.parse_speed_to_mbps("250 Mbps") == 250.0


def test_parse_speed_gbps_scales_up():
    assert engine.parse_speed_to_mbps("1 Gbps") == 1000.0


def test_parse_speed_kbps_scales_down():
    assert engine.parse_speed_to_mbps("500 Kbps") == 0.5


def test_parse_speed_handles_commas():
    assert engine.parse_speed_to_mbps("1,000 Mbps") == 1000.0


def test_parse_speed_empty_returns_none():
    assert engine.parse_speed_to_mbps("") is None


def test_parse_speed_garbage_returns_none():
    assert engine.parse_speed_to_mbps("fast") is None


# --- helpers to build SiteRun objects ------------------------------------

def _site(speed_mbps: float | None) -> SiteDefinition:
    return SiteDefinition(
        index=1, raw={}, placeholders={}, display_name="spoke-1",
        ip_address="10.0.0.1", hub_ip="10.0.0.254", speed="100M",
        speed_mbps=speed_mbps, speed_with_margin_mbps=None,
        speed_with_margin_label="115M",
    )


def _cmd(sender_mbps: float | None, return_code: int = 0, stdout: str = "ok") -> CommandResult:
    now = datetime(2026, 6, 18, 10, 0, 0)
    return CommandResult(
        template="traffictest run", command="traffictest run ...",
        started_at=now, ended_at=now, return_code=return_code,
        stdout=stdout, stderr="", sender_throughput_mbps=sender_mbps,
        throughput_mbps=sender_mbps,
    )


def _run(speed_mbps, sender_mbps, **cmd_kw) -> SiteRun:
    now = datetime(2026, 6, 18, 10, 0, 0)
    cmds = [] if sender_mbps is None and not cmd_kw else [_cmd(sender_mbps, **cmd_kw)]
    return SiteRun(site=_site(speed_mbps), started_at=now, ended_at=now, command_results=cmds)


# --- _compute_result -----------------------------------------------------

def test_result_pass_when_at_least_95_percent():
    # 96 >= 0.95 * 100 -> Pass
    label, css = engine._compute_result(_run(100.0, 96.0))
    assert (label, css) == ("Pass", "success")


def test_result_pass_exactly_at_threshold():
    label, css = engine._compute_result(_run(100.0, 95.0))
    assert css == "success"


def test_result_fail_insufficient_speed():
    label, css = engine._compute_result(_run(100.0, 80.0))
    assert css == "failed"
    assert "insufficient" in label


def test_result_fail_not_reachable_when_no_throughput():
    run = _run(100.0, None)  # no command results -> no sender throughput
    label, css = engine._compute_result(run)
    assert css == "failed"
    assert "not reachable" in label


# --- summarize -----------------------------------------------------------

def test_summarize_counts_pass_and_fail():
    runs = [
        _run(100.0, 96.0),   # pass
        _run(100.0, 50.0),   # fail (insufficient)
        _run(100.0, None),   # fail (unreachable)
    ]
    summary = engine.summarize(runs)
    assert summary["total_sites"] == 3
    assert summary["successful_sites"] == 1
    assert summary["failed_sites"] == 2


def test_summarize_empty():
    summary = engine.summarize([])
    assert summary == {
        "total_sites": 0, "successful_sites": 0, "failed_sites": 0,
        "peak_sender_mbps": None, "peak_receiver_mbps": None,
    }


# --- format helpers ------------------------------------------------------

def test_format_peak_none():
    assert engine.format_peak(None) == "N/A"


def test_format_peak_value():
    assert engine.format_peak(123.456) == "123.46 Mbps"


def test_format_seconds():
    assert engine.format_seconds(12.34) == "12.3s"


# --- CommandResult.status (iperf error detection) ------------------------

def test_command_status_success():
    assert _cmd(100.0, return_code=0, stdout="all good").status == "success"


def test_command_status_failed_on_iperf_error_text():
    assert _cmd(0.0, return_code=0, stdout="iperf3: error - unable to connect").status == "failed"


def test_command_status_failed_on_nonzero_return_code():
    assert _cmd(0.0, return_code=1, stdout="ok").status == "failed"
