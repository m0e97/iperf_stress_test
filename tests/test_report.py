"""Tests for the generated HTML report (build_html_report).

Covers (a) the report still renders valid-looking HTML with the data rows it is
given, and (b) the report theme colors remain in the stylesheet so a future
refactor doesn't silently drop the theme.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import main as engine
from main import CommandResult, SiteDefinition, SiteRun

# Accent + surface colors used in the report theme.
ACCENT = "#8c3d2b"
PANEL = "#fffdf9"


def _make_run(name: str, sender_mbps: float, speed_mbps: float = 100.0) -> SiteRun:
    now = datetime(2026, 6, 18, 10, 0, 0)
    site = SiteDefinition(
        index=1, raw={}, placeholders={}, display_name=name,
        ip_address="10.0.0.1", hub_ip="10.0.0.254", speed="100M",
        speed_mbps=speed_mbps, speed_with_margin_mbps=115.0,
        speed_with_margin_label="115M", circuit_id="C-1", isp="ISP-X",
    )
    cmd = CommandResult(
        template="traffictest run", command="traffictest run ...",
        started_at=now, ended_at=now, return_code=0, stdout="ok", stderr="",
        sender_throughput_mbps=sender_mbps, throughput_mbps=sender_mbps,
    )
    return SiteRun(site=site, started_at=now, ended_at=now, command_results=[cmd])


def _build(tmp_path: Path) -> str:
    runs = [_make_run("riyadh-1", 98.0), _make_run("jeddah-2", 40.0)]
    return engine.build_html_report(
        input_path=tmp_path / "in.csv",
        output_path=tmp_path / "out.html",
        results=runs,
        command_templates=["traffictest run --port {port}"],
        delay_seconds=5,
    )


def test_report_is_valid_html_document(tmp_path):
    html = _build(tmp_path)
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert "<table>" in html


def test_report_contains_site_rows_and_summary(tmp_path):
    html = _build(tmp_path)
    assert "riyadh-1" in html
    assert "jeddah-2" in html
    # Summary metric labels.
    assert "Total Sites" in html
    assert "Successful Sites" in html
    assert "Failed Sites" in html


def test_report_uses_theme_colors(tmp_path):
    """Regression: keep the report's accent + panel theme colors in the CSS."""
    html = _build(tmp_path).lower()
    assert ACCENT in html, "accent color missing from report stylesheet"
    assert PANEL in html, "panel color missing from report stylesheet"


def test_report_pass_fail_badges(tmp_path):
    html = _build(tmp_path)
    # 98 of 100 -> Pass; 40 of 100 -> Fail.
    assert "Pass" in html
    assert "Fail" in html
