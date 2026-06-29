"""Tests for the ISP Compliance Report computation."""
from __future__ import annotations

from datetime import timedelta

import clock
from webapp import db, isp_report


def _device(name, speed, isp, circuit=""):
    return db.create_device({
        "name": name, "spoke_ip": f"10.0.0.{name[-1]}", "hub_ip": "10.0.0.254",
        "speed": speed, "isp": isp, "circuit_id": circuit,
    })


def _run(rid, days_ago, sites):
    when = clock.now() - timedelta(days=days_ago)
    db.insert_run(run_id=rid, started_at=when, source="devices", input_name="", settings={})
    db.insert_run_sites(rid, sites)
    db.finalize_run(run_id=rid, status="done", exit_code=0, finished_at=when,
                    archive_filename=None, summary={})


def _site(dev_id, spoke, tp):
    return {"spoke_ip": spoke, "hub_ip": "10.0.0.254", "device_id": dev_id,
            "status": "Pass", "throughput_mbps": tp}


def test_list_isps_counts(fresh_db):
    _device("FW-1", "100M", "STC")
    _device("FW-2", "100M", "STC")
    _device("FW-3", "100M", "Mobily")
    isps = {r["isp"]: r["device_count"] for r in db.list_isps()}
    assert isps == {"STC": 2, "Mobily": 1}


def test_compliance_scoring_and_window(fresh_db):
    d1 = _device("FW-1", "100M", "STC")      # threshold @90% = 90
    d2 = _device("FW-2", "200M", "STC")      # threshold = 180
    # In-window runs
    _run("R1", 2, [_site(d1, "10.0.0.1", 95), _site(d2, "10.0.0.2", 150)])   # d1 met, d2 miss
    _run("R2", 10, [_site(d1, "10.0.0.1", 80), _site(d2, "10.0.0.2", 195)])  # d1 miss, d2 met
    # Out-of-window run (40 days ago) must be excluded from a 30-day window
    _run("R3", 40, [_site(d1, "10.0.0.1", 99)])

    rep = isp_report.compute_isp_report("STC", clock.now() - timedelta(days=30), clock.now(), 90.0)
    assert rep["total_tests"] == 4          # R3 excluded
    assert rep["total_met"] == 2
    assert rep["total_not_met"] == 2
    assert rep["overall_compliance_pct"] == 50.0
    by_name = {d["name"]: d for d in rep["devices"]}
    assert by_name["FW-1"]["threshold_mbps"] == 90.0
    assert by_name["FW-1"]["met"] == 1 and by_name["FW-1"]["not_met"] == 1
    assert by_name["FW-2"]["threshold_mbps"] == 180.0


def test_configurable_sla_changes_outcome(fresh_db):
    d1 = _device("FW-1", "100M", "STC")
    _run("R1", 1, [_site(d1, "10.0.0.1", 85)])  # 85 Mbps on a 100M contract
    # At 90% SLA (threshold 90) -> miss; at 80% SLA (threshold 80) -> met
    strict = isp_report.compute_isp_report("STC", clock.now() - timedelta(days=7), clock.now(), 90.0)
    lax = isp_report.compute_isp_report("STC", clock.now() - timedelta(days=7), clock.now(), 80.0)
    assert strict["devices"][0]["met"] == 0
    assert lax["devices"][0]["met"] == 1


def test_device_without_speed_is_unscored(fresh_db):
    d1 = _device("FW-1", "", "STC")  # no contracted speed
    _run("R1", 1, [_site(d1, "10.0.0.1", 50)])
    rep = isp_report.compute_isp_report("STC", clock.now() - timedelta(days=7), clock.now(), 90.0)
    dev = rep["devices"][0]
    assert dev["contract_known"] is False
    assert dev["compliance_pct"] is None
    assert dev["total_tests"] == 0          # not scored
    assert rep["overall_compliance_pct"] is None


def test_worst_performers_ordering(fresh_db):
    d1 = _device("FW-1", "100M", "STC")
    d2 = _device("FW-2", "100M", "STC")
    _run("R1", 1, [_site(d1, "10.0.0.1", 95), _site(d2, "10.0.0.2", 30)])
    rep = isp_report.compute_isp_report("STC", clock.now() - timedelta(days=7), clock.now(), 90.0)
    # Worst (lowest compliance) first
    assert rep["devices"][0]["name"] == "FW-2"
    assert rep["worst_performers"][0]["name"] == "FW-2"


def test_trend_buckets_reflect_improvement(fresh_db):
    d1 = _device("FW-1", "100M", "STC")  # threshold 90
    _run("R-old", 25, [_site(d1, "10.0.0.1", 60)])   # early: miss
    _run("R-mid", 14, [_site(d1, "10.0.0.1", 80)])   # mid: miss
    _run("R-new", 2, [_site(d1, "10.0.0.1", 96)])    # recent: met
    rep = isp_report.compute_isp_report("STC", clock.now() - timedelta(days=30), clock.now(), 90.0)
    scored = [b for b in rep["trend"] if b["total"]]
    assert len(scored) >= 2
    # Earliest scored bucket is a miss, latest is a pass -> trend improves.
    assert scored[0]["compliance_pct"] == 0.0
    assert scored[-1]["compliance_pct"] == 100.0


def test_all_isps_ranked_best_first(fresh_db):
    d1 = _device("FW-1", "100M", "STC")
    d2 = _device("FW-2", "100M", "Mobily")
    _run("R1", 1, [_site(d1, "10.0.0.1", 40), _site(d2, "10.0.0.2", 95)])
    summary = isp_report.compute_all_isps(clock.now() - timedelta(days=7), clock.now(), 90.0)
    names = [s["isp"] for s in summary["isps"]]
    assert names == ["Mobily", "STC"]            # 100% before 0%
    assert summary["isps"][0]["overall_compliance_pct"] == 100.0
    assert summary["isps"][1]["overall_compliance_pct"] == 0.0
    assert summary["total_tests"] == 2
