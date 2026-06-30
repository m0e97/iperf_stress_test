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

def test_result_pass_when_at_least_90_percent():
    # 91 >= 0.90 * 100 -> Pass
    label, css = engine._compute_result(_run(100.0, 91.0))
    assert (label, css) == ("Pass", "success")


def test_result_pass_exactly_at_default_threshold():
    # 90 >= 0.90 * 100 -> Pass
    label, css = engine._compute_result(_run(100.0, 90.0))
    assert css == "success"


def test_result_fail_insufficient_speed():
    label, css = engine._compute_result(_run(100.0, 80.0))
    assert css == "failed"
    assert "insufficient" in label


def test_result_uses_accepted_speed_override():
    # accepted_speed=70 means 80 Mbps passes even though it is below 90% of 100
    site = _site(100.0)
    site.accepted_speed = "70M"
    site.accepted_speed_mbps = 70.0
    run = SiteRun(
        site=site, started_at=datetime(2026, 6, 18, 10, 0, 0),
        ended_at=datetime(2026, 6, 18, 10, 0, 0), command_results=[_cmd(80.0)],
    )
    label, css = engine._compute_result(run)
    assert (label, css) == ("Pass", "success")


def test_result_fail_below_accepted_speed_override():
    # accepted_speed=95 makes 92 fail even though it clears the 90% default
    site = _site(100.0)
    site.accepted_speed = "95M"
    site.accepted_speed_mbps = 95.0
    run = SiteRun(
        site=site, started_at=datetime(2026, 6, 18, 10, 0, 0),
        ended_at=datetime(2026, 6, 18, 10, 0, 0), command_results=[_cmd(92.0)],
    )
    _, css = engine._compute_result(run)
    assert css == "failed"


def test_result_fail_not_reachable_when_no_throughput():
    run = _run(100.0, None)  # no command results -> no sender throughput
    label, css = engine._compute_result(run)
    assert css == "failed"
    assert "not reachable" in label


# --- summarize -----------------------------------------------------------

def test_summarize_counts_pass_and_fail():
    runs = [
        _run(100.0, 91.0),   # pass (>= 90% of 100)
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


# --- speed-test allowaccess pre-flight check -----------------------------

_SHOW_ALLOWED = """config system interface
    edit "Mobily"
        set allowaccess ping https ssh speed-test
    next
end"""
_SHOW_DENIED = """config system interface
    edit "Mobily"
        set allowaccess ping https ssh
    next
end"""


def test_speedtest_allowed_true():
    assert engine.speedtest_allowed(_SHOW_ALLOWED) is True


def test_speedtest_allowed_false():
    assert engine.speedtest_allowed(_SHOW_DENIED) is False


def test_speedtest_allowed_unparseable_is_none():
    assert engine.speedtest_allowed("edit port1\n next\n end") is None
    assert engine.speedtest_allowed("") is None


def test_speedtest_allowed_token_variants():
    assert engine.speedtest_allowed("set allowaccess ping speedtest") is True
    assert engine.speedtest_allowed("set allowaccess ping speed_test") is True
    # 'speed-test-foo' should not be a false positive; only the bare token counts
    assert engine.speedtest_allowed("set allowaccess ping https") is False


# --- routing pre-flight check --------------------------------------------

def test_routing_via_interface_match():
    out = "Routing entry for 10.255.0.1/32\n  * 10.0.0.1, via Mobily"
    assert engine.routing_via_interface(out, "Mobily") is True
    assert engine.routing_via_interface(out, "wan1") is False


def test_routing_via_interface_empty_is_false():
    assert engine.routing_via_interface("", "Mobily") is False
    assert engine.routing_via_interface("routing table is empty", "Mobily") is False
    assert engine.routing_via_interface("* via Mobily", "") is False


def test_routing_via_interface_word_boundary():
    # 'wan1' must not match inside 'wan10'
    assert engine.routing_via_interface("* via wan10", "wan1") is False
    assert engine.routing_via_interface("* via wan1", "wan1") is True


def _fake_shell_factory(monkeypatch, responder):
    sent = []

    class _Shell:
        def send(self, s): sent.append(s)
        def close(self): pass
        def settimeout(self, *a): pass

    monkeypatch.setattr(engine, "_paramiko_connect", lambda *a, **k: type("C", (), {"close": lambda s: None})())
    monkeypatch.setattr(engine, "_paramiko_open_shell", lambda *a, **k: _Shell())
    monkeypatch.setattr(engine, "_shell_read_until_prompt", lambda shell, timeout=None: responder(sent))
    return sent


def test_hub_routing_check_verdicts(monkeypatch):
    def responder(sent):
        last = sent[-1] if sent else ""
        if "10.0.0.1" in last:
            return "* 10.0.0.1, via Mobily"
        if "10.0.0.2" in last:
            return "* 10.0.0.2, via wan1"
        return ""
    _fake_shell_factory(monkeypatch, responder)
    s1 = engine.build_sites([{"spoke_ip": "10.0.0.1", "hub_ip": "10.255.0.1", "speed": "100M"}])[0]
    s2 = engine.build_sites([{"spoke_ip": "10.0.0.2", "hub_ip": "10.255.0.1", "speed": "100M"}])[0]
    results, verdicts = engine._paramiko_hub_routing_check("10.255.0.1", [s1, s2], "Mobily", timeout=10, dry_run=False)
    assert verdicts == {"10.0.0.1": True, "10.0.0.2": False}
    assert any(r.error for r in results)            # the failed spoke is flagged


def test_spoke_routing_gate_skips_test_when_wrong_interface(monkeypatch):
    def responder(sent):
        last = sent[-1] if sent else ""
        if "routing-table details" in last:
            return "* 10.255.0.1, via wan1"        # NOT the expected wan2
        return "iperf3 done"
    _fake_shell_factory(monkeypatch, responder)
    site = engine.build_sites([{"spoke_ip": "10.0.0.9", "hub_ip": "10.255.0.1", "speed": "100M"}])[0]
    site.placeholders.update({"spoke_client_intf": "wan2", "traffictest_port": "5201", "duration_flag": ""})
    res = engine._paramiko_spoke_session(site, engine.FORTIGATE_SPOKE_COMMANDS, 10, False, routing_intf="wan2")
    assert len(res) == 1 and res[0].error          # only the routing check ran; test skipped


def test_spoke_routing_gate_proceeds_when_correct(monkeypatch):
    def responder(sent):
        last = sent[-1] if sent else ""
        if "routing-table details" in last:
            return "* 10.255.0.1, via wan2"
        return "iperf3 done 95 Mbits/sec sender"
    _fake_shell_factory(monkeypatch, responder)
    site = engine.build_sites([{"spoke_ip": "10.0.0.9", "hub_ip": "10.255.0.1", "speed": "100M"}])[0]
    site.placeholders.update({"spoke_client_intf": "wan2", "traffictest_port": "5201", "duration_flag": ""})
    res = engine._paramiko_spoke_session(site, engine.FORTIGATE_SPOKE_COMMANDS, 10, False, routing_intf="wan2")
    assert len(res) == len(engine.FORTIGATE_SPOKE_COMMANDS) + 1   # routing check + all spoke commands


def test_hub_session_dry_run_includes_speedtest_check():
    site = engine.build_sites([{"spoke_ip": "10.0.0.1", "hub_ip": "10.0.0.254", "speed": "100M"}])[0]
    site.placeholders["hub_server_intf"] = "Mobily"
    site.placeholders["traffictest_port"] = "5201"
    setup, server, handle = engine._paramiko_hub_session(
        "10.0.0.254", site, engine.FORTIGATE_HUB_SETUP_COMMANDS,
        engine.FORTIGATE_HUB_SERVER_COMMAND, timeout=10, dry_run=True, check_speedtest=True,
    )
    assert setup[0].template == engine.FORTIGATE_HUB_ALLOWACCESS_CHECK
    # When disabled, the check is not added.
    setup2, _, _ = engine._paramiko_hub_session(
        "10.0.0.254", site, engine.FORTIGATE_HUB_SETUP_COMMANDS,
        engine.FORTIGATE_HUB_SERVER_COMMAND, timeout=10, dry_run=True, check_speedtest=False,
    )
    assert all(r.template != engine.FORTIGATE_HUB_ALLOWACCESS_CHECK for r in setup2)


# --- test duration / -t flag ---------------------------------------------

def test_sanitize_duration():
    assert engine._sanitize_duration("30") == "30"
    assert engine._sanitize_duration(45) == "45"
    assert engine._sanitize_duration("") == ""
    assert engine._sanitize_duration("0") == ""        # not positive
    assert engine._sanitize_duration("-5") == ""
    assert engine._sanitize_duration("abc") == ""
    assert engine._sanitize_duration(None) == ""


def _render_run_command(site_duration: str, global_duration):
    """Mirror main()'s per-site duration wiring, then render the run command."""
    site = engine.build_sites(
        [{"spoke_ip": "10.0.0.1", "hub_ip": "10.0.0.254", "speed": "100M",
          "traffictest_duration": site_duration}]
    )[0]
    site.traffictest_duration = engine._sanitize_duration(site.traffictest_duration) or (
        engine._sanitize_duration(global_duration) if global_duration else ""
    )
    site.placeholders["duration_flag"] = (
        f" -t {site.traffictest_duration}" if site.traffictest_duration else ""
    )
    template = engine.FORTIGATE_SPOKE_COMMANDS[-1]
    return template.format_map(engine.build_template_values(site, {}))


def test_run_command_omits_t_when_no_duration():
    cmd = _render_run_command("", None)
    assert "-t" not in cmd
    assert cmd.endswith("-c 10.0.0.254")


def test_run_command_appends_t_from_device():
    assert _render_run_command("30", None).endswith("-c 10.0.0.254 -t 30")


def test_run_command_uses_global_duration_when_device_blank():
    assert _render_run_command("", 20).endswith("-t 20")


def test_run_command_device_duration_overrides_global():
    assert _render_run_command("45", 20).endswith("-t 45")


def test_build_sites_reads_duration_alias():
    site = engine.build_sites(
        [{"spoke_ip": "10.0.0.1", "hub_ip": "10.0.0.254", "speed": "100M", "duration": "15"}]
    )[0]
    assert site.traffictest_duration == "15"
