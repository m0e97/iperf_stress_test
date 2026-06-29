"""ISP Compliance Report.

Aggregates historic per-site test results for every device of a chosen ISP over a
time window and scores each test against a configurable SLA percentage of the
device's contracted speed. Produces the data the UI renders plus standalone
HTML / Excel / PDF exports for contract-renewal negotiations.
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

import clock
import main as engine
from webapp import db


# --- computation ----------------------------------------------------------

def compute_isp_report(isp: str, start: datetime, end: datetime, sla_pct: float) -> dict[str, Any]:
    """Build the report structure for one ISP over [start, end) at the given SLA %.

    A test "meets" the SLA when its sender throughput >= sla_pct% of the device's
    contracted speed. Devices with no contracted speed are reported but left
    unscored (compliance shown as N/A).
    """
    start_iso = start.isoformat(timespec="seconds")
    end_iso = end.isoformat(timespec="seconds")
    rows = db.isp_run_rows(isp, start_iso, end_iso)
    devices = db.devices_for_isp(isp)
    ratio = sla_pct / 100.0

    buckets: dict[int, dict[str, Any]] = {}
    for d in devices:
        buckets[d["id"]] = {"device": d, "tests": [], "throughputs": []}
    for r in rows:
        b = buckets.get(r["device_id"])
        if b is None:
            continue
        b["tests"].append(r)
        if r["throughput_mbps"] is not None:
            b["throughputs"].append(r["throughput_mbps"])

    device_reports: list[dict[str, Any]] = []
    for b in buckets.values():
        d = b["device"]
        contract = engine.parse_speed_to_mbps(d.get("speed") or "")
        threshold = round(contract * ratio, 2) if contract is not None else None
        met = not_met = 0
        for t in b["tests"]:
            tp = t["throughput_mbps"]
            if tp is None or threshold is None:
                continue
            if tp >= threshold:
                met += 1
            else:
                not_met += 1
        scored = met + not_met
        tps = b["throughputs"]
        avg_tp = round(sum(tps) / len(tps), 2) if tps else None
        device_reports.append({
            "device_id": d["id"],
            "name": d.get("name") or "",
            "spoke_ip": d.get("spoke_ip") or "",
            "circuit_id": d.get("circuit_id") or "",
            "speed": d.get("speed") or "",
            "contract_mbps": contract,
            "threshold_mbps": threshold,
            "total_tests": scored,
            "met": met,
            "not_met": not_met,
            "compliance_pct": round(met / scored * 100, 1) if scored else None,
            "min_mbps": round(min(tps), 2) if tps else None,
            "avg_mbps": avg_tp,
            "max_mbps": round(max(tps), 2) if tps else None,
            "avg_pct_of_contract": round(avg_tp / contract * 100, 1) if (avg_tp is not None and contract) else None,
            "last_test": max((t["started_at"] for t in b["tests"]), default=None),
            "contract_known": contract is not None,
        })

    # Worst compliance first (devices with data), then unscored/no-data devices.
    device_reports.sort(key=lambda d: (d["compliance_pct"] is None, d["compliance_pct"] if d["compliance_pct"] is not None else 0, -d["total_tests"]))

    total_met = sum(d["met"] for d in device_reports)
    total_not_met = sum(d["not_met"] for d in device_reports)
    total_scored = total_met + total_not_met

    return {
        "isp": isp,
        "start": start,
        "end": end,
        "sla_pct": sla_pct,
        "generated_at": clock.now(),
        "devices": device_reports,
        "total_devices": len(device_reports),
        "devices_with_data": sum(1 for d in device_reports if d["total_tests"] > 0),
        "total_tests": total_scored,
        "total_met": total_met,
        "total_not_met": total_not_met,
        "overall_compliance_pct": round(total_met / total_scored * 100, 1) if total_scored else None,
        "worst_performers": [d for d in device_reports if d["compliance_pct"] is not None][:5],
    }


# --- helpers ---------------------------------------------------------------

def _fmt(v, suffix: str = "") -> str:
    return "—" if v is None else f"{v}{suffix}"


def _pct_class(pct, sla_pct) -> str:
    if pct is None:
        return "na"
    return "ok" if pct >= sla_pct else ("warn" if pct >= sla_pct * 0.8 else "bad")


def _window_label(report: dict[str, Any]) -> str:
    return f'{report["start"].strftime("%Y-%m-%d")} → {report["end"].strftime("%Y-%m-%d")}'


# --- HTML export (standalone) ---------------------------------------------

def render_html(report: dict[str, Any]) -> str:
    isp = html.escape(report["isp"])
    sla = report["sla_pct"]
    overall = report["overall_compliance_pct"]
    overall_cls = _pct_class(overall, sla)

    rows_html = []
    for d in report["devices"]:
        cls = _pct_class(d["compliance_pct"], sla)
        name = html.escape(d["name"] or d["spoke_ip"])
        rows_html.append(
            f"<tr>"
            f"<td>{name}<div class='sub'>{html.escape(d['spoke_ip'])}</div></td>"
            f"<td>{html.escape(d['circuit_id']) or '—'}</td>"
            f"<td>{html.escape(d['speed']) or '—'}</td>"
            f"<td class='num'>{_fmt(d['threshold_mbps'], ' Mbps')}</td>"
            f"<td class='num'>{d['total_tests']}</td>"
            f"<td class='num ok-t'>{d['met']}</td>"
            f"<td class='num bad-t'>{d['not_met']}</td>"
            f"<td class='num'><span class='pill {cls}'>{_fmt(d['compliance_pct'], '%')}</span></td>"
            f"<td class='num'>{_fmt(d['avg_mbps'])}</td>"
            f"<td class='num'>{_fmt(d['min_mbps'])} / {_fmt(d['max_mbps'])}</td>"
            f"<td class='num'>{_fmt(d['avg_pct_of_contract'], '%')}</td>"
            f"</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>ISP Compliance Report — {isp}</title>
<style>
  :root {{ --bg:#f4f6f9; --panel:#fff; --ink:#1a1f2e; --muted:#5a6478; --border:#dde2ec;
           --ok:#0e7a4a; --warn:#b8860b; --bad:#c0392b; --accent:#8c3d2b; --accent-dark:#6e2f20; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif; }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:28px; }}
  h1 {{ margin:0 0 4px; font-size:1.5rem; color:var(--accent-dark); }}
  .meta {{ color:var(--muted); margin-bottom:20px; }}
  .cards {{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:22px; }}
  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:14px 18px; min-width:150px; }}
  .card .label {{ color:var(--muted); font-size:.8rem; text-transform:uppercase; letter-spacing:.05em; }}
  .card .value {{ font-size:1.7rem; font-weight:700; }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--border); border-radius:10px; overflow:hidden; }}
  th,td {{ padding:9px 12px; border-bottom:1px solid var(--border); text-align:left; }}
  th {{ background:#eef2f7; font-size:.74rem; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  td.num, th.num {{ text-align:right; }}
  .sub {{ color:var(--muted); font-size:.8rem; }}
  .ok-t {{ color:var(--ok); }} .bad-t {{ color:var(--bad); }}
  .pill {{ display:inline-block; padding:2px 9px; border-radius:20px; font-weight:700; font-size:.85rem; }}
  .pill.ok {{ background:#dcf3e8; color:var(--ok); }}
  .pill.warn {{ background:#fbf0d5; color:var(--warn); }}
  .pill.bad {{ background:#f8dcd8; color:var(--bad); }}
  .pill.na {{ background:#eceff3; color:var(--muted); }}
  .value.ok {{ color:var(--ok); }} .value.warn {{ color:var(--warn); }} .value.bad {{ color:var(--bad); }}
  .note {{ color:var(--muted); font-size:.85rem; margin-top:16px; }}
</style></head><body><div class="wrap">
  <h1>ISP Compliance Report — {isp}</h1>
  <div class="meta">Window {_window_label(report)} · SLA target {sla:g}% of contracted speed · generated {report['generated_at'].strftime('%Y-%m-%d %H:%M')}</div>
  <div class="cards">
    <div class="card"><div class="label">Overall compliance</div><div class="value {overall_cls}">{_fmt(overall, '%')}</div></div>
    <div class="card"><div class="label">Devices</div><div class="value">{report['devices_with_data']}<span style="font-size:1rem;color:var(--muted)"> / {report['total_devices']}</span></div></div>
    <div class="card"><div class="label">Tests scored</div><div class="value">{report['total_tests']}</div></div>
    <div class="card"><div class="label">Met</div><div class="value ok">{report['total_met']}</div></div>
    <div class="card"><div class="label">Not met</div><div class="value bad">{report['total_not_met']}</div></div>
  </div>
  <table>
    <thead><tr>
      <th>Device</th><th>Circuit</th><th>Contract</th><th class="num">SLA threshold</th>
      <th class="num">Tests</th><th class="num">Met</th><th class="num">Not met</th>
      <th class="num">Compliance</th><th class="num">Avg Mbps</th><th class="num">Min / Max</th><th class="num">Avg % of contract</th>
    </tr></thead>
    <tbody>{''.join(rows_html)}</tbody>
  </table>
  <p class="note">Devices are ordered worst-compliance first. "Met" = sender throughput ≥ {sla:g}% of the device's contracted speed.
  Devices with no contracted speed configured are shown with N/A compliance. Devices listed with 0 tests had no runs in this window.</p>
</div></body></html>"""


# --- Excel export ----------------------------------------------------------

def build_excel(report: dict[str, Any], output_path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "ISP Compliance"
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="1F2532")
    head_font = Font(bold=True, color="FFFFFF")

    ws.append([f"ISP Compliance Report — {report['isp']}"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"Window {_window_label(report)}  |  SLA target {report['sla_pct']:g}% of contracted speed"])
    ws.append([f"Generated {report['generated_at'].strftime('%Y-%m-%d %H:%M')}"])
    ws.append([])
    ws.append(["Overall compliance", _fmt(report["overall_compliance_pct"], "%"),
               "Devices (with data / total)", f"{report['devices_with_data']} / {report['total_devices']}",
               "Tests", report["total_tests"], "Met", report["total_met"], "Not met", report["total_not_met"]])
    ws.append([])

    headers = ["Device", "Spoke IP", "Circuit", "Contract", "SLA threshold (Mbps)",
               "Tests", "Met", "Not met", "Compliance %", "Avg Mbps", "Min Mbps", "Max Mbps",
               "Avg % of contract", "Last test"]
    hrow = ws.max_row + 1
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=hrow, column=c)
        cell.fill = head_fill
        cell.font = head_font

    for d in report["devices"]:
        ws.append([
            d["name"] or d["spoke_ip"], d["spoke_ip"], d["circuit_id"], d["speed"],
            d["threshold_mbps"], d["total_tests"], d["met"], d["not_met"],
            d["compliance_pct"], d["avg_mbps"], d["min_mbps"], d["max_mbps"],
            d["avg_pct_of_contract"], (d["last_test"] or "")[:19].replace("T", " "),
        ])
    widths = [22, 15, 14, 12, 18, 8, 8, 9, 13, 11, 11, 11, 17, 19]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)


# --- PDF export ------------------------------------------------------------

def build_pdf(report: dict[str, Any], output_path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    title = ParagraphStyle("t", parent=styles["Title"], textColor=colors.HexColor("#6e2f20"), fontSize=18)
    sub = ParagraphStyle("s", parent=styles["Normal"], textColor=colors.HexColor("#5a6478"), fontSize=9)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(output_path), pagesize=landscape(A4),
                            leftMargin=1.2 * cm, rightMargin=1.2 * cm, topMargin=1.2 * cm, bottomMargin=1.2 * cm)
    story = [
        Paragraph(f"ISP Compliance Report — {html.escape(report['isp'])}", title),
        Paragraph(f"Window {_window_label(report)} &nbsp;|&nbsp; SLA target {report['sla_pct']:g}% of contracted speed "
                  f"&nbsp;|&nbsp; generated {report['generated_at'].strftime('%Y-%m-%d %H:%M')}", sub),
        Spacer(1, 8),
        Paragraph(f"<b>Overall compliance: {_fmt(report['overall_compliance_pct'], '%')}</b> &nbsp; "
                  f"Devices with data: {report['devices_with_data']}/{report['total_devices']} &nbsp; "
                  f"Tests: {report['total_tests']} &nbsp; Met: {report['total_met']} &nbsp; Not met: {report['total_not_met']}",
                  styles["Normal"]),
        Spacer(1, 10),
    ]

    data = [["Device", "Circuit", "Contract", "SLA thr.", "Tests", "Met", "Not met", "Compl.%", "Avg", "Min", "Max", "% contr."]]
    for d in report["devices"]:
        data.append([
            (d["name"] or d["spoke_ip"]), d["circuit_id"] or "—", d["speed"] or "—",
            _fmt(d["threshold_mbps"]), str(d["total_tests"]), str(d["met"]), str(d["not_met"]),
            _fmt(d["compliance_pct"]), _fmt(d["avg_mbps"]), _fmt(d["min_mbps"]), _fmt(d["max_mbps"]),
            _fmt(d["avg_pct_of_contract"]),
        ])
    tbl = Table(data, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E2532")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dde2ec")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fb")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    # Colour compliance cell by SLA outcome.
    sla = report["sla_pct"]
    for i, d in enumerate(report["devices"], start=1):
        c = d["compliance_pct"]
        if c is None:
            continue
        col = colors.HexColor("#0e7a4a") if c >= sla else (colors.HexColor("#b8860b") if c >= sla * 0.8 else colors.HexColor("#c0392b"))
        style.append(("TEXTCOLOR", (7, i), (7, i), col))
        style.append(("FONTNAME", (7, i), (7, i), "Helvetica-Bold"))
    tbl.setStyle(TableStyle(style))
    story.append(tbl)
    doc.build(story)
