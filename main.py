from __future__ import annotations

import argparse
import csv
import html
import re
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from string import Formatter
from typing import Any
from xml.etree import ElementTree as ET


NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
}

DEFAULT_DELAY_SECONDS = 120
THROUGHPUT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGTP]?bits/sec)", re.IGNORECASE)
ROLE_PATTERN = re.compile(r"\b(sender|receiver)\b", re.IGNORECASE)
NUMBER_PATTERN = re.compile(r"(\d+(?:\.\d+)?)")
NAME_ALIASES = {"name", "site", "site_name", "spoke", "spoke_name", "branch"}
IP_ALIASES = {"ip", "host", "address", "spoke_ip", "branch_ip", "wan_ip"}
SPEED_ALIASES = {
    "speed",
    "rate",
    "bandwidth",
    "expected_speed",
    "speed_mbps",
    "bandwidth_mbps",
}


@dataclass
class SiteDefinition:
    index: int
    raw: dict[str, str]
    placeholders: dict[str, str]
    display_name: str
    ip_address: str
    speed: str
    speed_mbps: float | None
    speed_with_margin_mbps: float | None
    speed_with_margin_label: str


@dataclass
class CommandResult:
    template: str
    command: str
    started_at: datetime
    ended_at: datetime
    return_code: int | None
    stdout: str
    stderr: str
    error: str | None = None
    throughput_mbps: float | None = None
    throughput_label: str | None = None

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def status(self) -> str:
        if self.error:
            return "template-error"
        if self.return_code == 0:
            return "success"
        return "failed"


@dataclass
class SiteRun:
    site: SiteDefinition
    started_at: datetime
    ended_at: datetime
    command_results: list[CommandResult] = field(default_factory=list)
    delayed_after_seconds: int = 0

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def status(self) -> str:
        if not self.command_results:
            return "skipped"
        if any(result.status != "success" for result in self.command_results):
            return "failed"
        return "success"

    @property
    def max_throughput_mbps(self) -> float | None:
        values = [
            result.throughput_mbps
            for result in self.command_results
            if result.throughput_mbps is not None
        ]
        if not values:
            return None
        return max(values)


class SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        raise KeyError(key)


def sanitize_key(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = value.strip("_")
    return value or "column"


def column_letters_to_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    index = 0
    for character in letters:
        index = (index * 26) + (ord(character.upper()) - ord("A") + 1)
    return index - 1


def load_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall("main:si", NS):
        parts = [node.text or "" for node in item.findall(".//main:t", NS)]
        strings.append("".join(parts))
    return strings


def resolve_sheet_path(archive: zipfile.ZipFile, sheet_name: str | None) -> tuple[str, str]:
    workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
    rel_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_map = {
        relation.attrib["Id"]: relation.attrib["Target"]
        for relation in rel_root.findall("pkg:Relationship", NS)
    }

    sheets = workbook_root.findall("main:sheets/main:sheet", NS)
    if not sheets:
        raise ValueError("No worksheets found in the Excel file.")

    selected = None
    if sheet_name:
        for sheet in sheets:
            if sheet.attrib.get("name") == sheet_name:
                selected = sheet
                break
        if selected is None:
            available = ", ".join(sheet.attrib.get("name", "<unnamed>") for sheet in sheets)
            raise ValueError(f"Worksheet '{sheet_name}' not found. Available sheets: {available}")
    else:
        selected = sheets[0]

    relationship_id = selected.attrib[f"{{{NS['rel']}}}id"]
    target = rel_map[relationship_id].lstrip("/")
    if not target.startswith("xl/"):
        target = f"xl/{target}"
    return target, selected.attrib.get("name", "Sheet1")


def extract_cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", NS))

    value_node = cell.find("main:v", NS)
    if value_node is None or value_node.text is None:
        return ""

    raw_value = value_node.text
    if cell_type == "s":
        return shared_strings[int(raw_value)]
    return raw_value


def load_xlsx_rows(path: Path, sheet_name: str | None) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        shared_strings = load_shared_strings(archive)
        sheet_path, _ = resolve_sheet_path(archive, sheet_name)
        sheet_root = ET.fromstring(archive.read(sheet_path))

    rows: list[dict[int, str]] = []
    for row in sheet_root.findall(".//main:sheetData/main:row", NS):
        parsed_row: dict[int, str] = {}
        for cell in row.findall("main:c", NS):
            reference = cell.attrib.get("r", "")
            column_index = column_letters_to_index(reference)
            parsed_row[column_index] = extract_cell_value(cell, shared_strings).strip()
        if any(value for value in parsed_row.values()):
            rows.append(parsed_row)

    if not rows:
        return []

    header_row = rows[0]
    max_index = max(header_row)
    headers = [header_row.get(index, "").strip() for index in range(max_index + 1)]

    records: list[dict[str, str]] = []
    for row in rows[1:]:
        record: dict[str, str] = {}
        for index, header in enumerate(headers):
            if header:
                record[header] = row.get(index, "").strip()
        if any(record.values()):
            records.append(record)
    return records


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{key.strip(): (value or "").strip() for key, value in row.items()} for row in reader]


def load_rows(path: Path, sheet_name: str | None) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_csv_rows(path)
    if suffix == ".xlsx":
        return load_xlsx_rows(path, sheet_name)
    raise ValueError("Only .csv and .xlsx input files are supported.")


def find_first_value(placeholders: dict[str, str], aliases: set[str]) -> str:
    for alias in aliases:
        if placeholders.get(alias):
            return placeholders[alias]
    return ""


def parse_speed_to_mbps(raw_speed: str) -> float | None:
    if not raw_speed:
        return None

    cleaned = raw_speed.strip().replace(",", "")
    match = NUMBER_PATTERN.search(cleaned)
    if not match:
        return None

    value = float(match.group(1))
    normalized = cleaned.lower().replace(" ", "")

    if any(unit in normalized for unit in ("gbps", "gbit", "gbits")) or normalized.endswith("g"):
        return value * 1000
    if any(unit in normalized for unit in ("kbps", "kbit", "kbits")) or normalized.endswith("k"):
        return value / 1000
    if any(unit in normalized for unit in ("bps", "bit", "bits")):
        return value / 1_000_000
    return value


def format_mbps_for_traffictest(speed_mbps: float | None) -> str:
    if speed_mbps is None:
        return ""

    rounded = round(speed_mbps, 2)
    if rounded.is_integer():
        return f"{int(rounded)}M"
    return f"{rounded:g}M"


def build_sites(rows: list[dict[str, str]]) -> list[SiteDefinition]:
    sites: list[SiteDefinition] = []
    for index, row in enumerate(rows, start=1):
        placeholders: dict[str, str] = {}
        for key, value in row.items():
            sanitized = sanitize_key(key)
            if sanitized in placeholders and placeholders[sanitized]:
                continue
            placeholders[sanitized] = value

        display_name = find_first_value(placeholders, NAME_ALIASES) or f"spoke-{index}"
        ip_address = find_first_value(placeholders, IP_ALIASES)
        speed = find_first_value(placeholders, SPEED_ALIASES)
        speed_mbps = parse_speed_to_mbps(speed)
        speed_with_margin_mbps = round(speed_mbps * 1.15, 2) if speed_mbps is not None else None
        speed_with_margin_label = format_mbps_for_traffictest(speed_with_margin_mbps)

        if speed_mbps is not None:
            placeholders.setdefault("speed_mbps", f"{speed_mbps:g}")
        if speed_with_margin_mbps is not None:
            placeholders.setdefault("speed_with_margin_mbps", f"{speed_with_margin_mbps:g}")
        if speed_with_margin_label:
            placeholders.setdefault("speed_with_margin", speed_with_margin_label)
            placeholders.setdefault("bandwidth_with_margin", speed_with_margin_label)

        sites.append(
            SiteDefinition(
                index=index,
                raw=row,
                placeholders=placeholders,
                display_name=display_name,
                ip_address=ip_address,
                speed=speed,
                speed_mbps=speed_mbps,
                speed_with_margin_mbps=speed_with_margin_mbps,
                speed_with_margin_label=speed_with_margin_label,
            )
        )
    return sites


def load_command_templates(args: argparse.Namespace) -> list[str]:
    templates = list(args.command or [])
    if args.command_file:
        file_path = Path(args.command_file)
        with file_path.open("r", encoding="utf-8") as handle:
            templates.extend(
                line.strip()
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            )
    templates = [template for template in templates if template]
    if not templates:
        raise ValueError("Provide at least one command with --command or --command-file.")
    return templates


def validate_template_fields(templates: list[str], available_keys: set[str]) -> None:
    missing_fields: set[str] = set()
    formatter = Formatter()
    for template in templates:
        for _, field_name, _, _ in formatter.parse(template):
            if field_name and field_name not in available_keys:
                missing_fields.add(field_name)
    if missing_fields:
        available = ", ".join(sorted(available_keys))
        missing = ", ".join(sorted(missing_fields))
        raise ValueError(f"Missing columns/placeholders: {missing}. Available placeholders: {available}")


def extract_throughput(output: str) -> tuple[float | None, str | None]:
    matches = list(THROUGHPUT_PATTERN.finditer(output))
    if not matches:
        return None, None

    chosen = matches[-1]
    value = float(chosen.group(1))
    unit = chosen.group(2).lower()
    multipliers = {
        "bits/sec": 1 / 1_000_000,
        "kbits/sec": 1 / 1_000,
        "mbits/sec": 1.0,
        "gbits/sec": 1_000.0,
        "tbits/sec": 1_000_000.0,
        "pbits/sec": 1_000_000_000.0,
    }
    throughput_mbps = value * multipliers[unit]
    nearby_text = output[max(0, chosen.start() - 60): min(len(output), chosen.end() + 60)]
    role_match = ROLE_PATTERN.search(nearby_text)
    label = f"{value:g} {chosen.group(2)}"
    if role_match:
        label = f"{label} ({role_match.group(1).lower()})"
    return throughput_mbps, label


def run_command(command: str, timeout: int | None, dry_run: bool) -> CommandResult:
    started_at = datetime.now()
    if dry_run:
        ended_at = datetime.now()
        return CommandResult(
            template=command,
            command=command,
            started_at=started_at,
            ended_at=ended_at,
            return_code=0,
            stdout="dry-run: command not executed",
            stderr="",
        )

    completed = subprocess.run(
        command,
        shell=True,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    ended_at = datetime.now()
    combined_output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
    throughput_mbps, throughput_label = extract_throughput(combined_output)
    return CommandResult(
        template=command,
        command=command,
        started_at=started_at,
        ended_at=ended_at,
        return_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        throughput_mbps=throughput_mbps,
        throughput_label=throughput_label,
    )


def render_command(template: str, site: SiteDefinition) -> str:
    values = SafeFormatDict(site.placeholders.copy())
    values.setdefault("site_index", str(site.index))
    values.setdefault("spoke_name", site.display_name)
    values.setdefault("site_name", site.display_name)
    values.setdefault("name", site.display_name)
    values.setdefault("spoke_ip", site.ip_address)
    values.setdefault("ip", site.ip_address)
    values.setdefault("speed", site.speed)
    values.setdefault("expected_speed", site.speed)
    values.setdefault("speed_mbps", f"{site.speed_mbps:g}" if site.speed_mbps is not None else "")
    values.setdefault(
        "speed_with_margin_mbps",
        f"{site.speed_with_margin_mbps:g}" if site.speed_with_margin_mbps is not None else "",
    )
    values.setdefault("speed_with_margin", site.speed_with_margin_label)
    values.setdefault("bandwidth_with_margin", site.speed_with_margin_label)
    return template.format_map(values)


def run_site(
    site: SiteDefinition,
    command_templates: list[str],
    timeout: int | None,
    dry_run: bool,
) -> SiteRun:
    started_at = datetime.now()
    results: list[CommandResult] = []

    for template in command_templates:
        try:
            command = render_command(template, site)
        except KeyError as error:
            ended_at = datetime.now()
            results.append(
                CommandResult(
                    template=template,
                    command="",
                    started_at=ended_at,
                    ended_at=ended_at,
                    return_code=None,
                    stdout="",
                    stderr="",
                    error=f"Missing placeholder value for '{error.args[0]}'",
                )
            )
            continue

        try:
            result = run_command(command, timeout=timeout, dry_run=dry_run)
        except subprocess.TimeoutExpired as error:
            ended_at = datetime.now()
            results.append(
                CommandResult(
                    template=template,
                    command=command,
                    started_at=started_at,
                    ended_at=ended_at,
                    return_code=None,
                    stdout=error.stdout or "",
                    stderr=error.stderr or "",
                    error=f"Timed out after {error.timeout} seconds",
                )
            )
            continue

        result.template = template
        results.append(result)

    ended_at = datetime.now()
    return SiteRun(site=site, started_at=started_at, ended_at=ended_at, command_results=results)


def summarize(results: list[SiteRun]) -> dict[str, Any]:
    total_commands = sum(len(site_run.command_results) for site_run in results)
    failed_sites = sum(1 for site_run in results if site_run.status != "success")
    successful_sites = len(results) - failed_sites
    throughput_values = [
        site_run.max_throughput_mbps
        for site_run in results
        if site_run.max_throughput_mbps is not None
    ]
    return {
        "total_sites": len(results),
        "total_commands": total_commands,
        "successful_sites": successful_sites,
        "failed_sites": failed_sites,
        "peak_throughput_mbps": max(throughput_values) if throughput_values else None,
    }


def format_timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def format_seconds(value: float) -> str:
    return f"{value:.1f}s"


def format_peak(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f} Mbps"


def build_html_report(
    input_path: Path,
    output_path: Path,
    results: list[SiteRun],
    command_templates: list[str],
    delay_seconds: int,
) -> str:
    summary = summarize(results)
    created_at = datetime.now()

    rows_html: list[str] = []
    details_html: list[str] = []

    for site_run in results:
        rows_html.append(
            """
            <tr>
              <td>{index}</td>
              <td>{name}</td>
              <td>{ip}</td>
              <td>{speed}</td>
              <td>{test_speed}</td>
              <td class="{status_class}">{status}</td>
              <td>{peak}</td>
              <td>{started}</td>
              <td>{duration}</td>
            </tr>
            """.format(
                index=site_run.site.index,
                name=html.escape(site_run.site.display_name),
                ip=html.escape(site_run.site.ip_address or "N/A"),
                speed=html.escape(site_run.site.speed or "N/A"),
                test_speed=html.escape(site_run.site.speed_with_margin_label or "N/A"),
                status=html.escape(site_run.status),
                status_class=html.escape(site_run.status),
                peak=html.escape(format_peak(site_run.max_throughput_mbps)),
                started=html.escape(format_timestamp(site_run.started_at)),
                duration=html.escape(format_seconds(site_run.duration_seconds)),
            )
        )

        command_blocks: list[str] = []
        for result in site_run.command_results:
            output_text = "\n".join(
                section
                for section in [
                    f"STDOUT:\n{result.stdout.strip()}" if result.stdout.strip() else "",
                    f"STDERR:\n{result.stderr.strip()}" if result.stderr.strip() else "",
                    f"ERROR:\n{result.error}" if result.error else "",
                ]
                if section
            ) or "No output captured."

            command_blocks.append(
                """
                <div class="command-block">
                  <div><strong>Template:</strong> <code>{template}</code></div>
                  <div><strong>Command:</strong> <code>{command}</code></div>
                  <div><strong>Status:</strong> <span class="{status_class}">{status}</span></div>
                  <div><strong>Return Code:</strong> {return_code}</div>
                  <div><strong>Started:</strong> {started}</div>
                  <div><strong>Duration:</strong> {duration}</div>
                  <div><strong>Detected Throughput:</strong> {throughput}</div>
                  <pre>{output}</pre>
                </div>
                """.format(
                    template=html.escape(result.template),
                    command=html.escape(result.command or "N/A"),
                    status=html.escape(result.status),
                    status_class=html.escape(result.status),
                    return_code=html.escape(str(result.return_code) if result.return_code is not None else "N/A"),
                    started=html.escape(format_timestamp(result.started_at)),
                    duration=html.escape(format_seconds(result.duration_seconds)),
                    throughput=html.escape(result.throughput_label or "N/A"),
                    output=html.escape(output_text),
                )
            )

        details_html.append(
            """
            <section class="site-card">
              <h2>{name}</h2>
              <p><strong>IP:</strong> {ip}</p>
              <p><strong>Configured Speed:</strong> {speed}</p>
              <p><strong>Traffic Test Bandwidth (+15%):</strong> {test_speed}</p>
              <p><strong>Site Status:</strong> <span class="{status_class}">{status}</span></p>
              <p><strong>Started:</strong> {started}</p>
              <p><strong>Ended:</strong> {ended}</p>
              <p><strong>Inter-site Delay Applied After This Site:</strong> {delay}s</p>
              {commands}
            </section>
            """.format(
                name=html.escape(site_run.site.display_name),
                ip=html.escape(site_run.site.ip_address or "N/A"),
                speed=html.escape(site_run.site.speed or "N/A"),
                test_speed=html.escape(site_run.site.speed_with_margin_label or "N/A"),
                status=html.escape(site_run.status),
                status_class=html.escape(site_run.status),
                started=html.escape(format_timestamp(site_run.started_at)),
                ended=html.escape(format_timestamp(site_run.ended_at)),
                delay=site_run.delayed_after_seconds,
                commands="\n".join(command_blocks),
            )
        )

    command_list = "".join(f"<li><code>{html.escape(template)}</code></li>" for template in command_templates)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SD-WAN Traffic Test Report</title>
  <style>
    :root {{
      --bg: #f6f3ee;
      --panel: #fffdf9;
      --text: #1f2933;
      --muted: #52606d;
      --border: #d9d2c6;
      --success: #116530;
      --failed: #9b1c1c;
      --template-error: #b26b00;
      --accent: #8c3d2b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Tahoma, sans-serif;
      background: linear-gradient(180deg, #f4efe7 0%, #fcfaf6 100%);
      color: var(--text);
      line-height: 1.5;
    }}
    main {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    h1, h2 {{ margin-top: 0; }}
    .hero, .summary, .site-card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 20px;
      box-shadow: 0 10px 24px rgba(31, 41, 51, 0.06);
      margin-bottom: 20px;
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin: 16px 0;
    }}
    .metric {{
      background: #faf6f0;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px;
    }}
    .metric-label {{
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .metric-value {{
      font-size: 1.35rem;
      font-weight: 700;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 16px;
      font-size: 0.95rem;
    }}
    th, td {{
      padding: 10px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
    }}
    code, pre {{
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 0.9rem;
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #fbf8f3;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px;
      overflow-x: auto;
    }}
    .command-block {{
      padding-top: 14px;
      margin-top: 14px;
      border-top: 1px solid var(--border);
    }}
    .success {{ color: var(--success); font-weight: 700; }}
    .failed {{ color: var(--failed); font-weight: 700; }}
    .template-error {{ color: var(--template-error); font-weight: 700; }}
    .muted {{ color: var(--muted); }}
    ul {{ margin: 0; padding-left: 20px; }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <h1>SD-WAN Traffic Test Report</h1>
      <p class="muted">Generated at {html.escape(format_timestamp(created_at))}</p>
      <p><strong>Input File:</strong> {html.escape(str(input_path))}</p>
      <p><strong>Report File:</strong> {html.escape(str(output_path))}</p>
      <p><strong>Inter-site Delay:</strong> {delay_seconds} seconds</p>
      <p><strong>Command Templates:</strong></p>
      <ul>{command_list}</ul>
    </section>

    <section class="summary">
      <h2>Summary</h2>
      <div class="summary-grid">
        <div class="metric"><div class="metric-label">Total Sites</div><div class="metric-value">{summary["total_sites"]}</div></div>
        <div class="metric"><div class="metric-label">Successful Sites</div><div class="metric-value">{summary["successful_sites"]}</div></div>
        <div class="metric"><div class="metric-label">Failed Sites</div><div class="metric-value">{summary["failed_sites"]}</div></div>
        <div class="metric"><div class="metric-label">Total Commands</div><div class="metric-value">{summary["total_commands"]}</div></div>
        <div class="metric"><div class="metric-label">Peak Detected Throughput</div><div class="metric-value">{html.escape(format_peak(summary["peak_throughput_mbps"]))}</div></div>
      </div>

      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Spoke</th>
            <th>IP</th>
            <th>Speed</th>
            <th>Test Bandwidth</th>
            <th>Status</th>
            <th>Peak Throughput</th>
            <th>Started</th>
            <th>Duration</th>
          </tr>
        </thead>
        <tbody>
          {"".join(rows_html)}
        </tbody>
      </table>
    </section>

    {"".join(details_html)}
  </main>
</body>
</html>
"""


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run SD-WAN spoke traffic tests sequentially from CSV/XLSX input and generate an HTML report."
    )
    parser.add_argument("--input", required=True, help="Path to a CSV or XLSX file containing spoke data.")
    parser.add_argument("--sheet", help="Worksheet name to read when the input file is XLSX.")
    parser.add_argument(
        "--command",
        action="append",
        help=(
            "Command template to run for each site. Useful placeholders include "
            "{spoke_name}, {spoke_ip}, {speed}, {speed_with_margin}, {speed_with_margin_mbps}."
        ),
    )
    parser.add_argument(
        "--command-file",
        help="Text file containing one command template per line. Blank lines and lines starting with # are ignored.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=int,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Delay between sites in seconds. Default: {DEFAULT_DELAY_SECONDS}.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Optional timeout per command in seconds.",
    )
    parser.add_argument(
        "--output",
        default="traffic_test_report.html",
        help="HTML report output path. Default: traffic_test_report.html",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render commands and report without executing the traffic tests.",
    )
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    if not input_path.exists():
        parser.error(f"Input file not found: {input_path}")

    if args.delay_seconds < 0:
        parser.error("--delay-seconds must be 0 or greater.")

    rows = load_rows(input_path, args.sheet)
    if not rows:
        parser.error("No spoke rows were found in the input file.")

    sites = build_sites(rows)
    command_templates = load_command_templates(args)
    available_placeholders = set(sites[0].placeholders)
    available_placeholders.update(
        {
            "site_index",
            "spoke_name",
            "site_name",
            "name",
            "spoke_ip",
            "ip",
            "speed",
            "expected_speed",
            "speed_mbps",
            "speed_with_margin_mbps",
            "speed_with_margin",
            "bandwidth_with_margin",
        }
    )
    validate_template_fields(command_templates, available_placeholders)

    runs: list[SiteRun] = []
    total_sites = len(sites)

    for index, site in enumerate(sites, start=1):
        print(f"[{index}/{total_sites}] Running site '{site.display_name}' ({site.ip_address or 'no-ip'})")
        site_run = run_site(site, command_templates, timeout=args.timeout, dry_run=args.dry_run)
        runs.append(site_run)

        if index < total_sites and args.delay_seconds:
            site_run.delayed_after_seconds = args.delay_seconds
            print(f"Waiting {args.delay_seconds} seconds before the next site...")
            time.sleep(args.delay_seconds)

    report_html = build_html_report(
        input_path=input_path,
        output_path=output_path,
        results=runs,
        command_templates=command_templates,
        delay_seconds=args.delay_seconds,
    )
    output_path.write_text(report_html, encoding="utf-8")

    summary = summarize(runs)
    print(f"Report written to: {output_path}")
    print(
        f"Sites: {summary['total_sites']}, success: {summary['successful_sites']}, "
        f"failed: {summary['failed_sites']}"
    )
    return 0 if summary["failed_sites"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
