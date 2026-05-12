from __future__ import annotations

import argparse
import csv
import ctypes
import ctypes.wintypes
import getpass
import html
import msvcrt
import re
import shlex
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from string import Formatter
from typing import Any
from xml.etree import ElementTree as ET

_PARAMIKO_IMPORT_ERROR: str = ""
try:
    import paramiko as _paramiko_lib
    _PARAMIKO_OK = True
except Exception as _e:
    _paramiko_lib = None  # type: ignore[assignment]
    _PARAMIKO_OK = False
    _PARAMIKO_IMPORT_ERROR = str(_e)

_use_paramiko: bool = False
_paramiko_user: str = ""
_paramiko_pass: str = ""


NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
}

DEFAULT_DELAY_SECONDS = 0
DEFAULT_TRAFFICTEST_PORT = "5201"
DEFAULT_HUB_SERVER_INTF = "Mobily"
DEFAULT_SPOKE_CLIENT_INTF = "wan1"
DEFAULT_HUB_SERVER_START_DELAY_SECONDS = 60.0
DEFAULT_TRAFFICTEST_DURATION_SECONDS = 60
DEFAULT_SSH_TEMPLATE = 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new {target} "{remote_command}"'
FORTIGATE_HUB_SETUP_COMMANDS = [
    "diagnose traffictest server-intf {hub_server_intf}",
    "diagnose traffictest port {traffictest_port}",
]
FORTIGATE_HUB_SERVER_COMMAND = "diagnose traffictest run -s"
FORTIGATE_SPOKE_COMMANDS = [
    "diagnose traffictest client-intf {spoke_client_intf}",
    "diagnose traffictest port {traffictest_port}",
    "diagnose traffictest run -b {speed_with_margin} -c {hub_ip} -t {traffictest_duration}",
]
THROUGHPUT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGTP]?bits/sec)", re.IGNORECASE)
ROLE_PATTERN = re.compile(r"\b(sender|receiver)\b", re.IGNORECASE)
# iperf3 summary line: [ 7]  0.00-10.02  sec  4.02 MBytes  3.37 Mbits/sec  72  sender
_IPERF3_SUMMARY_RE = re.compile(
    r"\[\s*\d+\]\s+[\d.]+-[\d.]+\s+sec\s+[\d.]+\s+\S+Bytes\s+"
    r"(\d+(?:\.\d+)?)\s*([KMGTP]?bits/sec)"
    r"(?:\s+(\d+))?\s*(sender|receiver)",
    re.IGNORECASE,
)
NUMBER_PATTERN = re.compile(r"(\d+(?:\.\d+)?)")
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_FORTIGATE_PROMPT_RE = re.compile(r"[#$]\s*$", re.MULTILINE)
_FORTIGATE_BANNER_RE = re.compile(
    r"press\s+['\"]?a['\"]?|type\s+['\"]?a['\"]?|accept.*disclaimer|disclaimer.*accept"
    r"|you\s+agree|acknowledge|post.?logon",
    re.IGNORECASE,
)
# iperf3 / traffictest errors that FortiGate reports in stdout with exit code 0
_IPERF_ERROR_RE = re.compile(
    r"iperf3?\s*:\s*error|unable\s+to\s+connect|network\s+is\s+unreachable"
    r"|connection\s+refused|connection\s+timed?\s*out|no\s+route\s+to\s+host"
    r"|failed\s+to\s+(connect|send|receive)",
    re.IGNORECASE,
)
FIREWALL_NAME_PATTERNS = [
    re.compile(r"^\s*hostname\s*[:=]\s*(?P<name>.+?)\s*$", re.IGNORECASE),
    re.compile(r"^\s*system\s+name\s*[:=]\s*(?P<name>.+?)\s*$", re.IGNORECASE),
    re.compile(r"^\s*sysname\s*[:=]\s*(?P<name>.+?)\s*$", re.IGNORECASE),
    re.compile(r"^\s*device\s+name\s*[:=]\s*(?P<name>.+?)\s*$", re.IGNORECASE),
    re.compile(r"""^\s*set\s+hostname\s+["']?(?P<name>[^"'\s]+)["']?\s*$""", re.IGNORECASE),
]
NAME_ALIASES = {"name", "site", "site_name", "spoke", "spoke_name", "branch"}
DISCOVERED_NAME_KEYS = NAME_ALIASES | {"firewall_name", "hostname", "device_name"}
IP_ALIASES = {"ip", "host", "address", "spoke_ip", "branch_ip", "wan_ip"}
HUB_IP_ALIASES = {"hub_ip", "hub", "hub_host", "hub_address", "hub_wan_ip"}
HUB_MGMT_IP_ALIASES = {"hub_mgmt_ip", "hub_management_ip", "hub_ssh_ip", "hub_admin_ip", "hub_mgmt"}
SPEED_ALIASES = {
    "speed",
    "rate",
    "bandwidth",
    "expected_speed",
    "speed_mbps",
    "bandwidth_mbps",
}
HUB_SERVER_INTF_ALIASES = {
    "hub_server_intf",
    "server_intf",
    "hub_intf",
    "hub_interface",
    "server_interface",
}
SPOKE_CLIENT_INTF_ALIASES = {
    "spoke_client_intf",
    "client_intf",
    "spoke_intf",
    "spoke_interface",
    "client_interface",
    "wan_intf",
    "wan_interface",
}
TRAFFICTEST_PORT_ALIASES = {
    "traffictest_port",
    "traffic_port",
    "iperf_port",
    "test_port",
}


@dataclass
class SiteDefinition:
    index: int
    raw: dict[str, str]
    placeholders: dict[str, str]
    display_name: str
    ip_address: str
    hub_ip: str
    speed: str
    speed_mbps: float | None
    speed_with_margin_mbps: float | None
    speed_with_margin_label: str
    hub_mgmt_ip: str = ""
    hub_server_intf: str = ""
    spoke_client_intf: str = ""
    traffictest_port: str = ""


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
    retransmissions: int | None = None

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def status(self) -> str:
        if self.error:
            return "template-error"
        if self.return_code == 0:
            if _IPERF_ERROR_RE.search(self.stdout) or _IPERF_ERROR_RE.search(self.stderr):
                return "failed"
            return "success"
        return "failed"


@dataclass
class SiteRun:
    site: SiteDefinition
    started_at: datetime
    ended_at: datetime
    command_results: list[CommandResult] = field(default_factory=list)
    name_discovery_result: CommandResult | None = None
    delayed_after_seconds: int = 0

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def status(self) -> str:
        if not self.command_results:
            if self.name_discovery_result and self.name_discovery_result.error:
                return "failed"
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

        ip_address = find_first_value(placeholders, IP_ALIASES)
        hub_ip = find_first_value(placeholders, HUB_IP_ALIASES)
        hub_mgmt_ip = find_first_value(placeholders, HUB_MGMT_IP_ALIASES)
        display_name = ip_address or f"spoke-{index}"
        for key in DISCOVERED_NAME_KEYS:
            placeholders[key] = display_name
        if hub_ip:
            placeholders.setdefault("hub_ip", hub_ip)
            placeholders.setdefault("hub", hub_ip)
        if hub_mgmt_ip:
            placeholders.setdefault("hub_mgmt_ip", hub_mgmt_ip)
        speed = find_first_value(placeholders, SPEED_ALIASES)
        hub_server_intf = find_first_value(placeholders, HUB_SERVER_INTF_ALIASES)
        spoke_client_intf = find_first_value(placeholders, SPOKE_CLIENT_INTF_ALIASES)
        traffictest_port = find_first_value(placeholders, TRAFFICTEST_PORT_ALIASES)
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
                hub_ip=hub_ip,
                hub_mgmt_ip=hub_mgmt_ip,
                speed=speed,
                speed_mbps=speed_mbps,
                speed_with_margin_mbps=speed_with_margin_mbps,
                speed_with_margin_label=speed_with_margin_label,
                hub_server_intf=hub_server_intf,
                spoke_client_intf=spoke_client_intf,
                traffictest_port=traffictest_port,
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
    return templates


def fortigate_traffictest_templates() -> list[str]:
    return FORTIGATE_HUB_SETUP_COMMANDS + [FORTIGATE_HUB_SERVER_COMMAND] + FORTIGATE_SPOKE_COMMANDS


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


def extract_throughput(output: str) -> tuple[float | None, str | None, int | None]:
    multipliers = {
        "bits/sec": 1 / 1_000_000,
        "kbits/sec": 1 / 1_000,
        "mbits/sec": 1.0,
        "gbits/sec": 1_000.0,
        "tbits/sec": 1_000_000.0,
        "pbits/sec": 1_000_000_000.0,
    }

    summary_matches = list(_IPERF3_SUMMARY_RE.finditer(output))
    if summary_matches:
        receiver = next((m for m in summary_matches if m.group(4).lower() == "receiver"), None)
        sender = next((m for m in summary_matches if m.group(4).lower() == "sender"), None)
        chosen = receiver or sender or summary_matches[-1]
        value = float(chosen.group(1))
        unit = chosen.group(2).lower()
        role = chosen.group(4).lower()
        throughput_mbps = value * multipliers[unit]
        label = f"{value:g} {chosen.group(2)} ({role})"
        retransmissions = int(sender.group(3)) if sender and sender.group(3) else None
        return throughput_mbps, label, retransmissions

    # Fall back to last generic bits/sec match (non-iperf3 output)
    matches = list(THROUGHPUT_PATTERN.finditer(output))
    if not matches:
        return None, None, None
    chosen = matches[-1]
    value = float(chosen.group(1))
    unit = chosen.group(2).lower()
    throughput_mbps = value * multipliers[unit]
    nearby_text = output[max(0, chosen.start() - 60): min(len(output), chosen.end() + 60)]
    role_match = ROLE_PATTERN.search(nearby_text)
    label = f"{value:g} {chosen.group(2)}"
    if role_match:
        label = f"{label} ({role_match.group(1).lower()})"
    return throughput_mbps, label, None


def clean_firewall_name(value: str) -> str | None:
    cleaned = ANSI_ESCAPE_PATTERN.sub("", value).strip().strip("'\"")
    cleaned = re.sub(r"\s*[>#]\s*$", "", cleaned).strip()
    if not cleaned:
        return None

    lowered = cleaned.lower()
    ignored_prefixes = (
        "warning:",
        "the authenticity of host",
        "permanently added",
        "pseudo-terminal",
        "last login",
        "welcome",
        "ssh:",
        "connection ",
        "permission denied",
        "command not found",
        "bash:",
        "zsh:",
    )
    if any(lowered.startswith(prefix) for prefix in ignored_prefixes):
        return None
    if len(cleaned) > 128:
        return None
    return cleaned


def parse_firewall_name(output: str) -> str | None:
    lines = [ANSI_ESCAPE_PATTERN.sub("", line).strip() for line in output.splitlines()]
    non_empty_lines = [line for line in lines if line]

    for line in non_empty_lines:
        for pattern in FIREWALL_NAME_PATTERNS:
            match = pattern.match(line)
            if match:
                candidate = clean_firewall_name(match.group("name"))
                if candidate:
                    return candidate

    if len(non_empty_lines) == 1:
        return clean_firewall_name(non_empty_lines[0])

    for line in non_empty_lines:
        candidate = clean_firewall_name(line)
        if candidate and re.fullmatch(r"[A-Za-z0-9_.:-]+", candidate):
            return candidate
    return None


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
    throughput_mbps, throughput_label, retransmissions = extract_throughput(combined_output)
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
        retransmissions=retransmissions,
    )


def build_ssh_command(
    site: SiteDefinition,
    ssh_template: str,
    target: str,
    remote_template: str,
) -> tuple[str, str]:
    remote_command = render_template(remote_template, site)
    command = render_template(
        ssh_template,
        site,
        {"target": target, "remote_command": remote_command},
    )
    return remote_template, command


def build_ssh_command_or_error(
    site: SiteDefinition,
    ssh_template: str,
    target: str,
    remote_template: str,
) -> tuple[str | None, CommandResult | None]:
    try:
        _, command = build_ssh_command(site, ssh_template, target, remote_template)
    except KeyError as error:
        moment = datetime.now()
        return None, CommandResult(
            template=remote_template,
            command="",
            started_at=moment,
            ended_at=moment,
            return_code=None,
            stdout="",
            stderr="",
            error=f"Missing placeholder value for '{error.args[0]}'",
        )
    return command, None


def run_rendered_command(
    template: str,
    command: str,
    timeout: int | None,
    dry_run: bool,
) -> CommandResult:
    try:
        result = run_command(command, timeout=timeout, dry_run=dry_run)
    except subprocess.TimeoutExpired as error:
        ended_at = datetime.now()
        return CommandResult(
            template=template,
            command=command,
            started_at=ended_at,
            ended_at=ended_at,
            return_code=None,
            stdout=error.stdout or "",
            stderr=error.stderr or "",
            error=f"Timed out after {error.timeout} seconds",
        )

    result.template = template
    return result


def start_background_command(
    template: str,
    command: str,
    dry_run: bool,
) -> tuple[CommandResult, subprocess.Popen[str] | None]:
    started_at = datetime.now()
    if dry_run:
        return (
            CommandResult(
                template=template,
                command=command,
                started_at=started_at,
                ended_at=datetime.now(),
                return_code=0,
                stdout="dry-run: background command not executed",
                stderr="",
            ),
            None,
        )

    process = subprocess.Popen(
        command,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return (
        CommandResult(
            template=template,
            command=command,
            started_at=started_at,
            ended_at=started_at,
            return_code=None,
            stdout="Hub server command started in the background.",
            stderr="",
        ),
        process,
    )


def finalize_background_command(
    initial_result: CommandResult,
    process: subprocess.Popen[str],
    stop_if_running: bool,
) -> CommandResult:
    stopped_after_test = False
    if process.poll() is None and stop_if_running:
        process.terminate()
        stopped_after_test = True

    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        stopped_after_test = True

    ended_at = datetime.now()
    return_code = 0 if stopped_after_test else process.returncode
    if stopped_after_test:
        stdout = "\n".join(
            part
            for part in [
                stdout.strip(),
                "Hub server process stopped after spoke traffictest commands.",
            ]
            if part
        )

    combined_output = "\n".join(part for part in [stdout, stderr] if part)
    throughput_mbps, throughput_label, retransmissions = extract_throughput(combined_output)
    return CommandResult(
        template=initial_result.template,
        command=initial_result.command,
        started_at=initial_result.started_at,
        ended_at=ended_at,
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        throughput_mbps=throughput_mbps,
        throughput_label=throughput_label,
        retransmissions=retransmissions,
    )


class _ParamikoHandle:
    """Drop-in for subprocess.Popen so finalize_background_command works unchanged."""

    def __init__(self) -> None:
        self._stdout_parts: list[str] = []
        self._stderr_parts: list[str] = []
        self._done = threading.Event()
        self._shell: Any = None
        self._client: Any = None
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode if self._done.is_set() else None

    def terminate(self) -> None:
        for obj in (self._shell, self._client):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass

    def kill(self) -> None:
        self.terminate()

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self._done.wait(timeout=timeout)
        return "".join(self._stdout_parts), "".join(self._stderr_parts)


def _paramiko_connect(host: str, timeout: int | None) -> Any:
    client = _paramiko_lib.SSHClient()
    client.set_missing_host_key_policy(_paramiko_lib.AutoAddPolicy())
    client.connect(host, username=_paramiko_user, password=_paramiko_pass, timeout=timeout or 10)
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(30)  # send SSH keepalive every 30 s to prevent idle disconnect
    return client


def _shell_recv_chunk(shell: Any) -> str:
    if shell.recv_ready():
        return shell.recv(4096).decode(errors="replace")
    return ""


def _shell_read_until_prompt(shell: Any, timeout: float) -> str:
    output = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = _shell_recv_chunk(shell)
        if chunk:
            output += chunk
            if _FORTIGATE_PROMPT_RE.search(ANSI_ESCAPE_PATTERN.sub("", output)):
                return output
        else:
            time.sleep(0.05)
    return output


def _paramiko_open_shell(client: Any, timeout: int | None) -> Any:
    """Open an interactive FortiGate shell and accept the post-logon banner if present."""
    shell = client.invoke_shell(width=220, height=50)
    shell.settimeout(timeout or 30)
    # Collect initial output for up to 5 s (banner, MOTD, etc.)
    initial = ""
    deadline = time.time() + 5
    while time.time() < deadline:
        chunk = _shell_recv_chunk(shell)
        if chunk:
            initial += chunk
        else:
            time.sleep(0.1)
        clean = ANSI_ESCAPE_PATTERN.sub("", initial)
        if _FORTIGATE_PROMPT_RE.search(clean):
            return shell  # prompt already visible — no banner
        if _FORTIGATE_BANNER_RE.search(clean):
            # Banner requires pressing 'a' to accept
            shell.send("a\n")
            _shell_read_until_prompt(shell, timeout=timeout or 30)
            return shell
    return shell


def _paramiko_exec(
    host: str,
    remote_command: str,
    template: str,
    timeout: int | None,
    dry_run: bool,
) -> CommandResult:
    cmd_label = f"[paramiko] {_paramiko_user}@{host}: {remote_command}"
    started_at = datetime.now()
    if dry_run:
        return CommandResult(
            template=template, command=cmd_label,
            started_at=started_at, ended_at=datetime.now(),
            return_code=0, stdout="dry-run: command not executed", stderr="",
        )
    try:
        client = _paramiko_connect(host, timeout)
        shell = _paramiko_open_shell(client, timeout)
        shell.send(remote_command + "\n")
        raw = _shell_read_until_prompt(shell, timeout=timeout or 60)
        shell.close()
        client.close()
        stdout = ANSI_ESCAPE_PATTERN.sub("", raw)
        rc = 0
    except Exception as exc:
        ended_at = datetime.now()
        return CommandResult(
            template=template, command=cmd_label,
            started_at=started_at, ended_at=ended_at,
            return_code=None, stdout="", stderr="", error=str(exc),
        )
    ended_at = datetime.now()
    throughput_mbps, throughput_label, retransmissions = extract_throughput(stdout)
    return CommandResult(
        template=template, command=cmd_label,
        started_at=started_at, ended_at=ended_at,
        return_code=rc, stdout=stdout, stderr="",
        throughput_mbps=throughput_mbps, throughput_label=throughput_label,
        retransmissions=retransmissions,
    )


def _paramiko_start_background(
    host: str,
    remote_command: str,
    template: str,
    dry_run: bool,
) -> tuple[CommandResult, _ParamikoHandle | None]:
    cmd_label = f"[paramiko] {_paramiko_user}@{host}: {remote_command}"
    started_at = datetime.now()
    if dry_run:
        return (
            CommandResult(
                template=template, command=cmd_label,
                started_at=started_at, ended_at=datetime.now(),
                return_code=0, stdout="dry-run: background command not executed", stderr="",
            ),
            None,
        )
    handle = _ParamikoHandle()

    def _run() -> None:
        try:
            client = _paramiko_connect(host, timeout=None)
            shell = _paramiko_open_shell(client, timeout=30)
            handle._shell = shell
            handle._client = client
            shell.send(remote_command + "\n")
            while not getattr(shell, "closed", False):
                chunk = _shell_recv_chunk(shell)
                if chunk:
                    handle._stdout_parts.append(chunk)
                else:
                    time.sleep(0.1)
        except Exception as exc:
            handle._stderr_parts.append(str(exc))
            handle.returncode = 1
        finally:
            handle.returncode = handle.returncode if handle.returncode is not None else 0
            handle._done.set()

    threading.Thread(target=_run, daemon=True).start()
    return (
        CommandResult(
            template=template, command=cmd_label,
            started_at=started_at, ended_at=started_at,
            return_code=None, stdout="Hub server command started via Paramiko.", stderr="",
        ),
        handle,
    )


def _paramiko_hub_session(
    ssh_target: str,
    rep_site: SiteDefinition,
    setup_templates: list[str],
    server_template: str,
    timeout: int | None,
    dry_run: bool,
) -> tuple[list[CommandResult], CommandResult, "_ParamikoHandle | None"]:
    """Run all hub commands in one Paramiko shell, entering global VDOM once."""
    cmd_prefix = f"[paramiko] {_paramiko_user}@{ssh_target}: "

    if dry_run:
        now = datetime.now()
        setup_results = [
            CommandResult(
                template=t, command=cmd_prefix + render_template(t, rep_site),
                started_at=now, ended_at=now,
                return_code=0, stdout="dry-run: command not executed", stderr="",
            )
            for t in setup_templates
        ]
        server_cmd_str = render_template(server_template, rep_site)
        server_initial = CommandResult(
            template=server_template, command=cmd_prefix + server_cmd_str,
            started_at=now, ended_at=now,
            return_code=0, stdout="dry-run: background command not executed", stderr="",
        )
        return setup_results, server_initial, None

    try:
        client = _paramiko_connect(ssh_target, timeout)
        shell = _paramiko_open_shell(client, timeout)
    except Exception as exc:
        now = datetime.now()
        err_result = CommandResult(
            template=setup_templates[0] if setup_templates else server_template,
            command=cmd_prefix + "(connect)",
            started_at=now, ended_at=now,
            return_code=None, stdout="", stderr="", error=str(exc),
        )
        return [err_result], err_result, None

    # Enter global VDOM once for the entire session.
    shell.send("config global\n")
    _shell_read_until_prompt(shell, timeout=timeout or 30)

    setup_results: list[CommandResult] = []
    connection_failed = False
    for template in setup_templates:
        rendered = render_template(template, rep_site)
        cmd_label = cmd_prefix + rendered
        started_at = datetime.now()
        try:
            shell.send(rendered + "\n")
            raw = _shell_read_until_prompt(shell, timeout=timeout or 60)
            stdout = ANSI_ESCAPE_PATTERN.sub("", raw)
            ended_at = datetime.now()
            setup_results.append(CommandResult(
                template=template, command=cmd_label,
                started_at=started_at, ended_at=ended_at,
                return_code=0, stdout=stdout, stderr="",
            ))
        except Exception as exc:
            ended_at = datetime.now()
            setup_results.append(CommandResult(
                template=template, command=cmd_label,
                started_at=started_at, ended_at=ended_at,
                return_code=None, stdout="", stderr="", error=str(exc),
            ))
            connection_failed = True
            break

    if connection_failed:
        try:
            shell.close()
            client.close()
        except Exception:
            pass
        server_initial = CommandResult(
            template=server_template, command=cmd_prefix + server_template,
            started_at=datetime.now(), ended_at=datetime.now(),
            return_code=None, stdout="", stderr="",
            error="Skipped — hub setup failed.",
        )
        return setup_results, server_initial, None

    # Start the server command on the same shell in a background thread.
    server_cmd_str = render_template(server_template, rep_site)
    cmd_label = cmd_prefix + server_cmd_str
    started_at = datetime.now()
    handle = _ParamikoHandle()
    handle._shell = shell
    handle._client = client

    def _read_server() -> None:
        try:
            # Remove the per-operation timeout so the shell stays open
            # indefinitely while waiting for spoke clients.
            shell.settimeout(None)
            shell.send(server_cmd_str + "\n")
            while not getattr(shell, "closed", False):
                chunk = _shell_recv_chunk(shell)
                if chunk:
                    handle._stdout_parts.append(chunk)
                else:
                    time.sleep(0.1)
        except Exception as exc:
            handle._stderr_parts.append(str(exc))
            handle.returncode = 1
        finally:
            handle.returncode = handle.returncode if handle.returncode is not None else 0
            handle._done.set()

    threading.Thread(target=_read_server, daemon=True).start()
    server_initial = CommandResult(
        template=server_template, command=cmd_label,
        started_at=started_at, ended_at=started_at,
        return_code=None, stdout="Hub server started in global VDOM via Paramiko.", stderr="",
    )
    return setup_results, server_initial, handle


def _exec_ssh(
    site: SiteDefinition,
    target: str,
    remote_template: str,
    ssh_template: str,
    timeout: int | None,
    dry_run: bool,
) -> CommandResult:
    if _use_paramiko:
        command, error = render_command_or_error(remote_template, site)
        if error is not None:
            return error
        return _paramiko_exec(target, command, remote_template, timeout, dry_run)
    command, error = build_ssh_command_or_error(site, ssh_template, target, remote_template)
    if error is not None:
        return error
    return run_rendered_command(remote_template, command, timeout=timeout, dry_run=dry_run)


def _exec_ssh_background(
    site: SiteDefinition,
    target: str,
    remote_template: str,
    ssh_template: str,
    dry_run: bool,
) -> tuple[CommandResult, subprocess.Popen[str] | _ParamikoHandle | None]:
    if _use_paramiko:
        command, error = render_command_or_error(remote_template, site)
        if error is not None:
            return error, None
        return _paramiko_start_background(target, command, remote_template, dry_run)
    command, error = build_ssh_command_or_error(site, ssh_template, target, remote_template)
    if error is not None:
        return error, None
    return start_background_command(remote_template, command, dry_run=dry_run)


def set_site_display_name(site: SiteDefinition, display_name: str) -> None:
    site.display_name = display_name
    for key in DISCOVERED_NAME_KEYS:
        site.placeholders[key] = display_name


def build_template_values(site: SiteDefinition, extra_values: dict[str, str] | None = None) -> SafeFormatDict:
    values = SafeFormatDict(site.placeholders.copy())
    values.setdefault("site_index", str(site.index))
    values.setdefault("spoke_name", site.display_name)
    values.setdefault("site_name", site.display_name)
    values.setdefault("name", site.display_name)
    values.setdefault("firewall_name", site.display_name)
    values.setdefault("hostname", site.display_name)
    values.setdefault("device_name", site.display_name)
    values.setdefault("spoke_ip", site.ip_address)
    values.setdefault("ip", site.ip_address)
    values.setdefault("hub_ip", site.hub_ip)
    values.setdefault("hub", site.hub_ip)
    values.setdefault("speed", site.speed)
    values.setdefault("expected_speed", site.speed)
    values.setdefault("speed_mbps", f"{site.speed_mbps:g}" if site.speed_mbps is not None else "")
    values.setdefault(
        "speed_with_margin_mbps",
        f"{site.speed_with_margin_mbps:g}" if site.speed_with_margin_mbps is not None else "",
    )
    values.setdefault("speed_with_margin", site.speed_with_margin_label)
    values.setdefault("bandwidth_with_margin", site.speed_with_margin_label)
    if extra_values:
        values.update(extra_values)
    return values


def render_template(
    template: str,
    site: SiteDefinition,
    extra_values: dict[str, str] | None = None,
) -> str:
    return template.format_map(build_template_values(site, extra_values))


def render_command(template: str, site: SiteDefinition) -> str:
    return render_template(template, site)


def render_command_or_error(
    template: str,
    site: SiteDefinition,
) -> tuple[str | None, CommandResult | None]:
    try:
        command = render_command(template, site)
    except KeyError as error:
        moment = datetime.now()
        return None, CommandResult(
            template=template,
            command="",
            started_at=moment,
            ended_at=moment,
            return_code=None,
            stdout="",
            stderr="",
            error=f"Missing placeholder value for '{error.args[0]}'",
        )
    return command, None


def discover_firewall_name(
    site: SiteDefinition,
    command_template: str,
    timeout: int | None,
    dry_run: bool,
) -> tuple[CommandResult, str | None]:
    if _use_paramiko:
        result = _paramiko_exec(site.ip_address, "get system status", "get system status", timeout, dry_run)
    else:
        command, error_result = render_command_or_error(command_template, site)
        if error_result is not None:
            return error_result, None
        result = run_rendered_command(command_template, command, timeout=timeout, dry_run=dry_run)

    if result.error or dry_run:
        return result, None

    discovered_name = parse_firewall_name(result.stdout)
    if discovered_name is None and result.return_code == 0:
        discovered_name = parse_firewall_name(result.stderr)
    return result, discovered_name


def run_site(
    site: SiteDefinition,
    command_templates: list[str],
    timeout: int | None,
    dry_run: bool,
    name_discovery_result: CommandResult | None = None,
) -> SiteRun:
    started_at = datetime.now()
    results: list[CommandResult] = []

    for template in command_templates:
        command, error_result = render_command_or_error(template, site)
        if error_result is not None:
            results.append(error_result)
            continue
        results.append(run_rendered_command(template, command, timeout=timeout, dry_run=dry_run))

    ended_at = datetime.now()
    return SiteRun(
        site=site,
        started_at=started_at,
        ended_at=ended_at,
        command_results=results,
        name_discovery_result=name_discovery_result,
    )


def run_fortigate_traffictest_site(
    site: SiteDefinition,
    args: argparse.Namespace,
    name_discovery_result: CommandResult | None = None,
) -> SiteRun:
    started_at = datetime.now()
    results: list[CommandResult] = []
    hub_server_process: subprocess.Popen[str] | None = None
    hub_server_result_index: int | None = None

    for remote_template in FORTIGATE_HUB_SETUP_COMMANDS:
        command, error_result = build_ssh_command_or_error(site, args.ssh_template, site.hub_ip, remote_template)
        if error_result is not None:
            results.append(error_result)
            continue
        results.append(run_rendered_command(remote_template, command, timeout=args.timeout, dry_run=args.dry_run))

    command, error_result = build_ssh_command_or_error(
        site, args.ssh_template, site.hub_ip, FORTIGATE_HUB_SERVER_COMMAND
    )
    if error_result is not None:
        results.append(error_result)
    else:
        initial_result, hub_server_process = start_background_command(
            FORTIGATE_HUB_SERVER_COMMAND, command, dry_run=args.dry_run
        )
        hub_server_result_index = len(results)
        results.append(initial_result)

    if hub_server_process is not None and hub_server_result_index is not None:
        time.sleep(args.hub_server_start_delay)
        if hub_server_process.poll() is not None:
            results[hub_server_result_index] = finalize_background_command(
                results[hub_server_result_index],
                hub_server_process,
                stop_if_running=False,
            )
            hub_server_process = None

    for remote_template in FORTIGATE_SPOKE_COMMANDS:
        command, error_result = build_ssh_command_or_error(site, args.ssh_template, site.ip_address, remote_template)
        if error_result is not None:
            results.append(error_result)
            continue
        results.append(run_rendered_command(remote_template, command, timeout=args.timeout, dry_run=args.dry_run))

    if hub_server_process is not None and hub_server_result_index is not None:
        results[hub_server_result_index] = finalize_background_command(
            results[hub_server_result_index],
            hub_server_process,
            stop_if_running=True,
        )

    ended_at = datetime.now()
    return SiteRun(
        site=site,
        started_at=started_at,
        ended_at=ended_at,
        command_results=results,
        name_discovery_result=name_discovery_result,
    )


def run_fortigate_spoke_only(
    site: SiteDefinition,
    args: argparse.Namespace,
    name_discovery_result: CommandResult | None = None,
) -> SiteRun:
    started_at = datetime.now()
    results: list[CommandResult] = []

    for remote_template in FORTIGATE_SPOKE_COMMANDS:
        results.append(
            _exec_ssh(site, site.ip_address, remote_template, args.ssh_template, args.timeout, args.dry_run)
        )

    ended_at = datetime.now()
    return SiteRun(
        site=site,
        started_at=started_at,
        ended_at=ended_at,
        command_results=results,
        name_discovery_result=name_discovery_result,
    )


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


def _render_command_block(result: CommandResult, heading: str = "Template") -> str:
    output_text = "\n".join(
        section
        for section in [
            f"STDOUT:\n{result.stdout.strip()}" if result.stdout.strip() else "",
            f"STDERR:\n{result.stderr.strip()}" if result.stderr.strip() else "",
            f"ERROR:\n{result.error}" if result.error else "",
        ]
        if section
    ) or "No output captured."
    throughput_line = (
        f'<div><strong>Detected Throughput:</strong> {html.escape(result.throughput_label)}</div>'
        if result.throughput_label is not None else ""
    )
    retr_line = (
        f'<div><strong>Retransmissions:</strong> {result.retransmissions}</div>'
        if result.retransmissions is not None else ""
    )
    return """
        <div class="command-block">
          <div><strong>{heading}:</strong> <code>{template}</code></div>
          <div><strong>Command:</strong> <code>{command}</code></div>
          <div><strong>Status:</strong> <span class="{status_class}">{status}</span></div>
          <div><strong>Return Code:</strong> {return_code}</div>
          <div><strong>Started:</strong> {started}</div>
          <div><strong>Duration:</strong> {duration}</div>
          {throughput_line}
          {retr_line}
          <pre>{output}</pre>
        </div>
        """.format(
        heading=html.escape(heading),
        template=html.escape(result.template),
        command=html.escape(result.command or "N/A"),
        status=html.escape(result.status),
        status_class=html.escape(result.status),
        return_code=html.escape(str(result.return_code) if result.return_code is not None else "N/A"),
        started=html.escape(format_timestamp(result.started_at)),
        duration=html.escape(format_seconds(result.duration_seconds)),
        throughput_line=throughput_line,
        retr_line=retr_line,
        output=html.escape(output_text),
    )


def build_html_report(
    input_path: Path,
    output_path: Path,
    results: list[SiteRun],
    command_templates: list[str],
    delay_seconds: int,
    hub_results: dict[str, dict] | None = None,
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
              <td>{hub_ip}</td>
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
                hub_ip=html.escape(site_run.site.hub_ip or "N/A"),
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
        if site_run.name_discovery_result is not None:
            command_blocks.append(_render_command_block(site_run.name_discovery_result, "Firewall Name Discovery"))
        for result in site_run.command_results:
            command_blocks.append(_render_command_block(result))

        details_html.append(
            """
            <section class="site-card">
              <h2>{name}</h2>
              <p><strong>IP:</strong> {ip}</p>
              <p><strong>Hub IP:</strong> {hub_ip}</p>
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
                hub_ip=html.escape(site_run.site.hub_ip or "N/A"),
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

    hub_cards_html: list[str] = []
    for hub_ip, ctx in (hub_results or {}).items():
        hub_blocks: list[str] = []
        for r in ctx.get("setup_results", []):
            hub_blocks.append(_render_command_block(r, "Hub Setup"))
        server_result: CommandResult | None = ctx.get("server_result")
        if server_result is not None:
            hub_blocks.append(_render_command_block(server_result, "Hub Server"))
        ssh_target = ctx.get("ssh_target") or hub_ip
        mgmt_line = (
            f'<p><strong>Management IP (SSH):</strong> {html.escape(ssh_target)}</p>'
            if ssh_target != hub_ip else ""
        )
        hub_cards_html.append(
            """
            <section class="site-card">
              <h2>Hub: {hub_ip}</h2>
              {mgmt_line}
              {commands}
            </section>
            """.format(
                hub_ip=html.escape(hub_ip),
                mgmt_line=mgmt_line,
                commands="\n".join(hub_blocks),
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
      <h1>FortiGate Traffic Test Report</h1>
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
            <th>Firewall</th>
            <th>IP</th>
            <th>Hub IP</th>
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

    {"".join(hub_cards_html)}
    {"".join(details_html)}
  </main>
</body>
</html>
"""


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run FortiGate hub/spoke traffictest commands sequentially from CSV/XLSX input and generate an HTML report."
    )
    parser.add_argument("--input", default=None, help="Path to a CSV or XLSX file containing spoke data.")
    parser.add_argument("--sheet", help="Worksheet name to read when the input file is XLSX.")
    parser.add_argument(
        "--command",
        action="append",
        help=(
            "Optional custom command template to run for each site. If omitted, the built-in "
            "FortiGate diagnose traffictest hub/spoke flow is used. Useful placeholders include "
            "{firewall_name}, {spoke_name}, {spoke_ip}, {speed}, "
            "{speed_with_margin}, {speed_with_margin_mbps}, and {hub_ip}."
        ),
    )
    parser.add_argument(
        "--command-file",
        help="Text file containing one command template per line. Blank lines and lines starting with # are ignored.",
    )
    parser.add_argument(
        "--firewall-name-command",
        default='ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new {spoke_ip} "get system status"',
        help=(
            "SSH command template used to discover the firewall name before each test. "
            "The output may contain a line like 'Hostname: FW-01' or just the hostname. "
            "Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--firewall-name-timeout",
        type=int,
        default=30,
        help="Timeout in seconds for firewall name discovery. Default: 30.",
    )
    parser.add_argument(
        "--sshuser",
        help="SSH username to prepend to every target (e.g. admin).",
    )
    parser.add_argument(
        "--sshpw",
        nargs="?",
        const=True,
        default=None,
        metavar="PASSWORD",
        help=(
            "SSH password. Supply it directly as a value, or pass the flag with no value "
            "to be prompted interactively with hidden characters. Uses sshpass on Linux/macOS "
            "and plink on Windows (see --plink)."
        ),
    )
    parser.add_argument(
        "--plink",
        nargs="?",
        const="plink",
        default=None,
        metavar="PATH",
        help=(
            "Use PuTTY plink instead of ssh/sshpass. Optionally supply the full path to "
            "plink.exe; omit the value to use 'plink' from PATH. When combined with "
            "--sshpw, passes -pw to plink instead of using sshpass. "
            "Note: host keys must be pre-accepted in PuTTY's registry cache before "
            "running in batch mode."
        ),
    )
    parser.add_argument(
        "--paramiko",
        action="store_true",
        default=False,
        help=(
            "Use the Paramiko Python library for SSH instead of external binaries. "
            "Requires: pip install paramiko. Recommended on Windows where sshpass/plink "
            "are unavailable or blocked by group policy."
        ),
    )
    parser.add_argument(
        "--hub-ip",
        help="Hub firewall IP address used by spokes for iperf3. If omitted, each row must provide a hub_ip column.",
    )
    parser.add_argument(
        "--hub-mgmt-ip",
        help=(
            "Hub management IP address used for SSH (setup and server commands). "
            "Falls back to hub_ip when omitted. Can also be set per row with a hub_mgmt_ip column."
        ),
    )
    parser.add_argument(
        "--ssh-template",
        default=DEFAULT_SSH_TEMPLATE,
        help=(
            "SSH wrapper template for built-in FortiGate traffictest commands. "
            "Available placeholders: {target}, {remote_command}, plus site placeholders. "
            "Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--hub-server-intf",
        default=DEFAULT_HUB_SERVER_INTF,
        help=(
            "Fallback hub interface for 'diagnose traffictest server-intf' when the input row has no "
            f"server_intf/hub_server_intf column. Default: {DEFAULT_HUB_SERVER_INTF}."
        ),
    )
    parser.add_argument(
        "--spoke-client-intf",
        default=DEFAULT_SPOKE_CLIENT_INTF,
        help=(
            "Fallback spoke interface for 'diagnose traffictest client-intf' when the input row has no "
            f"client_intf/spoke_client_intf column. Default: {DEFAULT_SPOKE_CLIENT_INTF}."
        ),
    )
    parser.add_argument(
        "--traffictest-port",
        default=DEFAULT_TRAFFICTEST_PORT,
        help=(
            "Fallback FortiGate traffictest port when the input row has no "
            f"traffictest_port/traffic_port column. Default: {DEFAULT_TRAFFICTEST_PORT}."
        ),
    )
    parser.add_argument(
        "--traffictest-duration",
        type=int,
        default=DEFAULT_TRAFFICTEST_DURATION_SECONDS,
        help=(
            "Duration in seconds for each spoke traffic test (-t flag passed to "
            f"diagnose traffictest run). Default: {DEFAULT_TRAFFICTEST_DURATION_SECONDS}."
        ),
    )
    parser.add_argument(
        "--hub-server-start-delay",
        type=float,
        default=DEFAULT_HUB_SERVER_START_DELAY_SECONDS,
        help=(
            "Seconds to wait after starting the hub traffictest server before running spoke commands. "
            f"Default: {DEFAULT_HUB_SERVER_START_DELAY_SECONDS}."
        ),
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
        "--skip-hub-setup",
        action="store_true",
        help=(
            "Skip all hub SSH commands (config global, server-intf, port, run -s). "
            "Use this when you have already started the hub traffictest server manually. "
            "The script will run only the spoke-side test commands."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render commands and report without executing the traffic tests.",
    )
    return parser


def _read_password_hidden(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    # Try to disable echo via Windows console API (works in real consoles and
    # ConPTY terminals such as Windows Terminal or VSCode integrated terminal).
    kernel32 = ctypes.windll.kernel32
    h_stdin = kernel32.GetStdHandle(-10)
    old_mode = ctypes.wintypes.DWORD()
    echo_disabled = bool(
        kernel32.GetConsoleMode(h_stdin, ctypes.byref(old_mode))
        and kernel32.SetConsoleMode(h_stdin, old_mode.value & ~0x0004)
    )
    if not echo_disabled:
        # stdin is a pipe or IDE console (PyCharm run panel, etc.) — just use
        # plain input; characters will be visible.
        return input()
    try:
        chars: list[str] = []
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x08":
                if chars:
                    chars.pop()
                    sys.stderr.write("\b \b")
                    sys.stderr.flush()
            else:
                chars.append(ch)
                sys.stderr.write("*")
                sys.stderr.flush()
        sys.stderr.write("\n")
        sys.stderr.flush()
        return "".join(chars)
    except Exception:
        return input()
    finally:
        kernel32.SetConsoleMode(h_stdin, old_mode.value)


def prompt_interactive_inputs(args: argparse.Namespace) -> None:
    print("=" * 60)
    print("FortiGate Traffic Test Runner — Interactive Mode")
    print("=" * 60)
    while True:
        raw = input("Input file path (CSV or XLSX): ").strip().strip('"').strip("'")
        if raw:
            args.input = raw
            break
        print("  File path cannot be empty.")
    username = input("SSH username (leave blank to skip): ").strip()
    if username:
        args.sshuser = username
    plink_path = input("plink.exe path (leave blank to use standard ssh): ").strip().strip('"').strip("'")
    if plink_path:
        args.plink = plink_path
    if not args.plink:
        use_paramiko_answer = input("Use Paramiko for SSH? Needed on Windows without ssh/plink [y/N]: ").strip().lower()
        if use_paramiko_answer == "y":
            args.paramiko = True
    password = _read_password_hidden("SSH password (leave blank to skip): ")
    if password:
        args.sshpw = password
    skip_hub_answer = input("Hub traffictest server already running? Skip hub setup? [y/N]: ").strip().lower()
    if skip_hub_answer == "y":
        args.skip_hub_setup = True


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    if args.input is None:
        prompt_interactive_inputs(args)

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    if not input_path.exists():
        parser.error(f"Input file not found: {input_path}")

    if args.delay_seconds < 0:
        parser.error("--delay-seconds must be 0 or greater.")
    if args.firewall_name_timeout < 1:
        parser.error("--firewall-name-timeout must be 1 or greater.")
    if args.hub_server_start_delay < 0:
        parser.error("--hub-server-start-delay must be 0 or greater.")

    if args.paramiko or args.plink or args.sshuser or args.sshpw is not None:
        password: str | None = None
        if args.sshpw is not None:
            password = getpass.getpass("SSH password: ") if args.sshpw is True else args.sshpw

        if args.paramiko:
            if not _PARAMIKO_OK:
                detail = f" (import error: {_PARAMIKO_IMPORT_ERROR})" if _PARAMIKO_IMPORT_ERROR else ""
                parser.error(
                    f"--paramiko requires Paramiko. Install it with: pip install paramiko{detail}\n"
                    f"  Running interpreter: {sys.executable}"
                )
            global _use_paramiko, _paramiko_user, _paramiko_pass
            _use_paramiko = True
            _paramiko_user = args.sshuser or ""
            _paramiko_pass = password or ""
        else:
            user_at = f"{args.sshuser}@" if args.sshuser else ""
            if args.plink:
                pw_part = f" -pw {shlex.quote(password)}" if password else ""
                ssh_base = f"{shlex.quote(args.plink)} -batch -ssh{pw_part}"
            elif password:
                ssh_base = f"sshpass -p {shlex.quote(password)} ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
            else:
                ssh_base = "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
            args.ssh_template = f'{ssh_base} {user_at}{{target}} "{{remote_command}}"'
            args.firewall_name_command = f'{ssh_base} {user_at}{{spoke_ip}} "get system status"'

    rows = load_rows(input_path, args.sheet)
    if not rows:
        parser.error("No spoke rows were found in the input file.")

    sites = build_sites(rows)
    for site in sites:
        if args.hub_ip:
            site.hub_ip = args.hub_ip
            site.placeholders["hub_ip"] = args.hub_ip
            site.placeholders["hub"] = args.hub_ip
        if args.hub_mgmt_ip:
            site.hub_mgmt_ip = args.hub_mgmt_ip
            site.placeholders["hub_mgmt_ip"] = args.hub_mgmt_ip
        site.hub_server_intf = site.hub_server_intf or args.hub_server_intf
        site.spoke_client_intf = site.spoke_client_intf or args.spoke_client_intf
        site.traffictest_port = site.traffictest_port or str(args.traffictest_port)
        site.placeholders["hub_server_intf"] = site.hub_server_intf
        site.placeholders["spoke_client_intf"] = site.spoke_client_intf
        site.placeholders["traffictest_port"] = site.traffictest_port
        site.placeholders["traffic_port"] = site.traffictest_port
        site.placeholders["traffictest_duration"] = str(args.traffictest_duration)

    command_templates = load_command_templates(args)
    use_builtin_traffictest = not command_templates
    active_command_templates = fortigate_traffictest_templates() if use_builtin_traffictest else command_templates

    if use_builtin_traffictest:
        missing_hub_rows = [str(site.index) for site in sites if not site.hub_ip]
        if missing_hub_rows:
            parser.error(
                "The built-in FortiGate traffictest flow requires --hub-ip or a hub_ip column. "
                f"Missing hub IP for row(s): {', '.join(missing_hub_rows)}"
            )
        missing_speed_rows = [str(site.index) for site in sites if not site.speed_with_margin_label]
        if missing_speed_rows:
            parser.error(
                "The built-in FortiGate traffictest flow requires a speed value for every row. "
                f"Missing/invalid speed for row(s): {', '.join(missing_speed_rows)}"
            )

    available_placeholders = set(sites[0].placeholders)
    available_placeholders.update(
        {
            "site_index",
            "spoke_name",
            "site_name",
            "name",
            "firewall_name",
            "hostname",
            "device_name",
            "spoke_ip",
            "ip",
            "hub_ip",
            "hub",
            "speed",
            "expected_speed",
            "speed_mbps",
            "speed_with_margin_mbps",
            "speed_with_margin",
            "bandwidth_with_margin",
            "hub_server_intf",
            "spoke_client_intf",
            "traffictest_port",
            "traffic_port",
            "traffictest_duration",
        }
    )
    validate_template_fields(active_command_templates, available_placeholders)
    validate_template_fields([args.firewall_name_command], available_placeholders)
    if use_builtin_traffictest and not _use_paramiko:
        validate_template_fields(
            [args.ssh_template],
            available_placeholders | {"target", "remote_command"},
        )

    runs: list[SiteRun] = []
    hub_contexts: dict[str, dict] = {}
    total_sites = len(sites)

    if use_builtin_traffictest:
        # Collect unique hub IPs in the order they first appear, with a representative site each.
        seen_hubs: dict[str, SiteDefinition] = {}
        for site in sites:
            if site.hub_ip and site.hub_ip not in seen_hubs:
                seen_hubs[site.hub_ip] = site

        # Group spokes by hub IP into per-hub queues, preserving the original row order.
        hub_queues: dict[str, list[SiteDefinition]] = {hub_ip: [] for hub_ip in seen_hubs}
        for site in sites:
            hub_queues[site.hub_ip].append(site)

        if not args.skip_hub_setup:
            # Setup every hub in parallel: run the two setup commands then start the server.
            hub_contexts_lock = threading.Lock()

        def _setup_one_hub(hub_ip: str, rep_site: SiteDefinition) -> None:
            ssh_target = rep_site.hub_mgmt_ip or hub_ip

            if _use_paramiko:
                # One shell session: config global once, then all commands.
                setup_results, server_initial, server_process = _paramiko_hub_session(
                    ssh_target, rep_site,
                    FORTIGATE_HUB_SETUP_COMMANDS, FORTIGATE_HUB_SERVER_COMMAND,
                    args.timeout, args.dry_run,
                )
                connection_failed = any(r.error for r in setup_results) or (
                    server_initial is not None and server_initial.error == "Skipped — hub setup failed."
                )
            else:
                # Subprocess: each SSH call is a fresh session, so prepend
                # "config global" transparently to the remote command.
                setup_results = []
                connection_failed = False
                for template in FORTIGATE_HUB_SETUP_COMMANDS:
                    result = _exec_ssh(
                        rep_site, ssh_target,
                        "config global\n" + template,
                        args.ssh_template, args.timeout, args.dry_run,
                    )
                    # Report uses the clean template, not the prefixed one.
                    result = CommandResult(
                        template=template, command=result.command,
                        started_at=result.started_at, ended_at=result.ended_at,
                        return_code=result.return_code,
                        stdout=result.stdout, stderr=result.stderr,
                        error=result.error,
                    )
                    setup_results.append(result)
                    if result.error:
                        connection_failed = True
                        break

                server_initial = None
                server_process = None
                if not connection_failed:
                    server_initial, server_process = _exec_ssh_background(
                        rep_site, ssh_target,
                        "config global\n" + FORTIGATE_HUB_SERVER_COMMAND,
                        args.ssh_template, args.dry_run,
                    )
                    if server_initial is not None:
                        server_initial = CommandResult(
                            template=FORTIGATE_HUB_SERVER_COMMAND,
                            command=server_initial.command,
                            started_at=server_initial.started_at, ended_at=server_initial.ended_at,
                            return_code=server_initial.return_code,
                            stdout=server_initial.stdout, stderr=server_initial.stderr,
                            error=server_initial.error,
                        )

            with hub_contexts_lock:
                hub_contexts[hub_ip] = {
                    "ssh_target": ssh_target,
                    "setup_results": setup_results,
                    "server_initial": server_initial,
                    "server_process": server_process,
                    "failed": connection_failed,
                }

        if not args.skip_hub_setup:
            print(f"Starting traffictest server on {len(seen_hubs)} hub(s) in parallel...", flush=True)
            setup_threads = [
                threading.Thread(target=_setup_one_hub, args=(hub_ip, rep_site), daemon=True)
                for hub_ip, rep_site in seen_hubs.items()
            ]
            for t in setup_threads:
                t.start()
            for t in setup_threads:
                t.join()

            # Detect any hub server that exited before the delay (error condition).
            for _, ctx in hub_contexts.items():
                proc = ctx["server_process"]
                if proc is not None and proc.poll() is not None:
                    ctx["server_initial"] = finalize_background_command(
                        ctx["server_initial"], proc, stop_if_running=False
                    )
                    ctx["server_process"] = None

            print(f"Waiting {args.hub_server_start_delay:.0f}s for all hub servers to be ready...", flush=True)
            time.sleep(args.hub_server_start_delay)
        else:
            print("Hub setup skipped — assuming hub traffictest server is already running.", flush=True)

        # Run each hub's queue in its own thread (hubs run in parallel; spokes per hub run
        # sequentially so only one spoke at a time is active against each hub server).
        all_runs: list[SiteRun] = []
        all_runs_lock = threading.Lock()
        print_lock = threading.Lock()

        def _run_hub_queue(hub_ip: str, spoke_sites: list[SiteDefinition]) -> None:
            ctx = hub_contexts.get(hub_ip, {})
            if ctx.get("failed") and not args.dry_run:
                with print_lock:
                    print(
                        f"  [Hub {hub_ip}] Hub setup failed — skipping all {len(spoke_sites)} spoke(s).",
                        flush=True,
                    )
                now = datetime.now()
                with all_runs_lock:
                    for site in spoke_sites:
                        all_runs.append(SiteRun(site=site, started_at=now, ended_at=now))
                return

            queue_size = len(spoke_sites)
            for q_index, site in enumerate(spoke_sites, start=1):
                with print_lock:
                    print(
                        f"  [Hub {hub_ip}] [{q_index}/{queue_size}] Discovering name"
                        f" ({site.ip_address or 'no-ip'})",
                        flush=True,
                    )
                name_result, discovered_name = discover_firewall_name(
                    site, args.firewall_name_command,
                    timeout=args.firewall_name_timeout, dry_run=args.dry_run,
                )
                if discovered_name:
                    set_site_display_name(site, discovered_name)
                elif not args.dry_run:
                    with print_lock:
                        print(
                            f"  [Hub {hub_ip}] Could not discover firewall name;"
                            f" using fallback '{site.display_name}'.",
                            flush=True,
                        )

                if name_result.error and not args.dry_run:
                    with print_lock:
                        print(
                            f"  [Hub {hub_ip}] [{q_index}/{queue_size}] Skipping spoke"
                            f" '{site.display_name}' ({site.ip_address or 'no-ip'})"
                            f" — SSH connection failed: {name_result.error}",
                            flush=True,
                        )
                    now = datetime.now()
                    site_run = SiteRun(
                        site=site,
                        started_at=now,
                        ended_at=now,
                        name_discovery_result=name_result,
                    )
                else:
                    with print_lock:
                        print(
                            f"  [Hub {hub_ip}] [{q_index}/{queue_size}] Running spoke"
                            f" '{site.display_name}' ({site.ip_address or 'no-ip'})",
                            flush=True,
                        )
                    site_run = run_fortigate_spoke_only(site, args, name_discovery_result=name_result)

                if q_index < queue_size and args.delay_seconds:
                    site_run.delayed_after_seconds = args.delay_seconds
                    with print_lock:
                        print(
                            f"  [Hub {hub_ip}] Waiting {args.delay_seconds}s before next spoke...",
                            flush=True,
                        )
                    time.sleep(args.delay_seconds)

                with all_runs_lock:
                    all_runs.append(site_run)

        print(f"Running spoke tests across {len(hub_queues)} hub queue(s) in parallel...", flush=True)
        queue_threads = [
            threading.Thread(target=_run_hub_queue, args=(hub_ip, spoke_sites), daemon=True)
            for hub_ip, spoke_sites in hub_queues.items()
        ]
        for t in queue_threads:
            t.start()
        for t in queue_threads:
            t.join()

        # Sort collected runs back to the original CSV/XLSX row order.
        runs = sorted(all_runs, key=lambda r: r.site.index)

        if not args.skip_hub_setup:
            # Finalize all hub servers (stop the process and collect output).
            for _, ctx in hub_contexts.items():
                proc = ctx["server_process"]
                if proc is not None and ctx["server_initial"] is not None:
                    ctx["server_result"] = finalize_background_command(
                        ctx["server_initial"], proc, stop_if_running=True
                    )
                    ctx["server_process"] = None
                else:
                    ctx["server_result"] = ctx.get("server_initial")

    else:
        # Custom command mode: run sites sequentially.
        for index, site in enumerate(sites, start=1):
            print(f"[{index}/{total_sites}] Discovering firewall name ({site.ip_address or 'no-ip'})", flush=True)
            name_result, discovered_name = discover_firewall_name(
                site,
                args.firewall_name_command,
                timeout=args.firewall_name_timeout,
                dry_run=args.dry_run,
            )
            if discovered_name:
                set_site_display_name(site, discovered_name)
            elif not args.dry_run:
                print(f"  Could not discover firewall name; using fallback '{site.display_name}'.", flush=True)

            print(f"[{index}/{total_sites}] Running site '{site.display_name}' ({site.ip_address or 'no-ip'})", flush=True)
            site_run = run_site(
                site,
                command_templates,
                timeout=args.timeout,
                dry_run=args.dry_run,
                name_discovery_result=name_result,
            )
            runs.append(site_run)

            if index < total_sites and args.delay_seconds:
                site_run.delayed_after_seconds = args.delay_seconds
                print(f"Waiting {args.delay_seconds} seconds before the next site...", flush=True)
                time.sleep(args.delay_seconds)

    report_html = build_html_report(
        input_path=input_path,
        output_path=output_path,
        results=runs,
        command_templates=active_command_templates,
        delay_seconds=args.delay_seconds,
        hub_results=hub_contexts,
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
