from __future__ import annotations

import asyncio
import calendar
import csv
import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("IPERF_DATA_DIR", str(ROOT / "data"))).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "app.db"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT))
import main as engine  # noqa: E402

from webapp import db, ftp_archive, scheduler, serialize  # noqa: E402

db.init_db(DB_PATH)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# --- Job state ------------------------------------------------------------

@dataclass
class JobState:
    id: str
    status: str = "pending"
    exit_code: int | None = None
    error_message: str | None = None
    log_lines: list[str] = field(default_factory=list)
    log_lock: threading.Lock = field(default_factory=threading.Lock)
    new_line_event: threading.Event = field(default_factory=threading.Event)
    input_name: str = ""
    source: str = "csv"
    archive_filename: str | None = None
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    # Filled by the engine.build_html_report / engine.summarize monkey-patches
    # so we can serialize the run to FTP after _run_tests returns.
    captured_runs: Any = None
    captured_summary: dict[str, Any] = field(default_factory=dict)
    captured_templates: list[str] = field(default_factory=list)
    captured_delay: int = 0
    device_ids: list[int] = field(default_factory=list)

    def append_line(self, line: str) -> None:
        with self.log_lock:
            self.log_lines.append(line)
        self.new_line_event.set()

    def snapshot_from(self, offset: int) -> list[str]:
        with self.log_lock:
            return self.log_lines[offset:]


JOBS: dict[str, JobState] = {}
JOBS_LOCK = threading.RLock()
ACTIVE_JOB_ID: str | None = None


class _QueueTee:
    def __init__(self, original, job: JobState) -> None:
        self._original = original
        self._job = job
        self._buf = ""

    def write(self, text: str) -> int:
        try:
            self._original.write(text)
        except Exception:
            pass
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._job.append_line(line)
        return len(text)

    def flush(self) -> None:
        try:
            self._original.flush()
        except Exception:
            pass

    def fileno(self) -> int:
        return self._original.fileno()


# --- Argv builder ---------------------------------------------------------

def _build_argv(
    *,
    input_path: Path,
    output_path: Path,
    sshuser: str,
    hub_ip: str,
    hub_mgmt_ip: str,
    hub_server_intf: str,
    spoke_client_intf: str,
    traffictest_port: str,
    traffictest_duration: int,
    hub_server_start_delay: float,
    delay_seconds: int,
    timeout: int | None,
    skip_hub_setup: bool,
    dry_run: bool,
) -> list[str]:
    argv: list[str] = [
        "--input", str(input_path),
        "--output", str(output_path),
        "--paramiko",
        "--hub-server-intf", hub_server_intf or engine.DEFAULT_HUB_SERVER_INTF,
        "--spoke-client-intf", spoke_client_intf or engine.DEFAULT_SPOKE_CLIENT_INTF,
        "--traffictest-port", traffictest_port or engine.DEFAULT_TRAFFICTEST_PORT,
        "--traffictest-duration", str(traffictest_duration),
        "--hub-server-start-delay", str(hub_server_start_delay),
        "--delay-seconds", str(delay_seconds),
    ]
    if sshuser:
        argv += ["--sshuser", sshuser]
    if hub_ip:
        argv += ["--hub-ip", hub_ip]
    if hub_mgmt_ip:
        argv += ["--hub-mgmt-ip", hub_mgmt_ip]
    if timeout is not None and timeout > 0:
        argv += ["--timeout", str(timeout)]
    if skip_hub_setup:
        argv += ["--skip-hub-setup"]
    if dry_run:
        argv += ["--dry-run"]
    return argv


# --- Run worker -----------------------------------------------------------

def _install_capture_hook(job: JobState):
    """Wrap engine.build_html_report so we can capture runs + summary for archiving."""
    orig_html = engine.build_html_report
    orig_summarize = engine.summarize

    def html_wrapper(**kwargs):
        job.captured_runs = list(kwargs.get("results") or [])
        job.captured_templates = list(kwargs.get("command_templates") or [])
        job.captured_delay = int(kwargs.get("delay_seconds") or 0)
        return orig_html(**kwargs)

    def summarize_wrapper(results):
        summary = orig_summarize(results)
        job.captured_summary = summary
        return summary

    engine.build_html_report = html_wrapper
    engine.summarize = summarize_wrapper
    return orig_html, orig_summarize


def _uninstall_capture_hook(orig_html, orig_summarize) -> None:
    engine.build_html_report = orig_html
    engine.summarize = orig_summarize


def _run_job(
    job: JobState,
    argv: list[str],
    password: str,
    input_path: Path,
    settings: dict[str, Any],
) -> None:
    parser = engine.build_argument_parser()
    args = parser.parse_args(argv)
    args.sshpw = password if password else None

    db.insert_run(
        run_id=job.id,
        started_at=job.started_at,
        source=job.source,
        input_name=job.input_name,
        settings=settings,
    )
    db.update_run_status(job.id, "running")

    original_stdout, original_stderr = sys.stdout, sys.stderr
    tee_out = _QueueTee(original_stdout, job)
    tee_err = _QueueTee(original_stderr, job)
    sys.stdout = tee_out
    sys.stderr = tee_err

    orig_html, orig_summarize = _install_capture_hook(job)

    job.status = "running"
    job.append_line(f"$ python main.py {' '.join(shlex.quote(a) for a in argv)}")
    try:
        rc = engine._run_tests(args, parser)
        job.exit_code = int(rc) if isinstance(rc, int) else 0
        job.status = "done"
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        job.exit_code = code
        job.status = "done" if code == 0 else "error"
        if code != 0:
            job.error_message = f"Exited with code {code}."
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error_message = f"{type(exc).__name__}: {exc}"
        job.exit_code = 1
        job.append_line(f"ERROR: {job.error_message}")
    finally:
        _uninstall_capture_hook(orig_html, orig_summarize)
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        job.finished_at = datetime.now()

        # Push results to FTP archive if we captured them.
        if job.captured_runs:
            try:
                payload = serialize.serialize_runs(
                    job.captured_runs,
                    job.captured_summary,
                    input_path=input_path,
                    command_templates=job.captured_templates,
                    delay_seconds=job.captured_delay,
                )
                archive_name = f"{job.id}.json"
                ftp_archive.push_bytes(archive_name, json.dumps(payload).encode("utf-8"))
                job.archive_filename = archive_name
                job.append_line(f"Archive uploaded to FTP: {archive_name}")
            except Exception as exc:  # noqa: BLE001
                job.append_line(f"WARNING: FTP archive upload failed: {exc}")

            # Build run_sites rows from the captured runs.
            site_rows = []
            for site_run in job.captured_runs:
                site_rows.append({
                    "spoke_ip": site_run.site.ip_address or "",
                    "hub_ip": site_run.site.hub_ip or "",
                    "device_id": None,  # filled in by lookup, overridden below for devices source
                    "status": site_run.status,
                    "display_name": site_run.site.display_name or "",
                    "throughput_mbps": site_run.max_throughput_mbps,
                })
            # If source is 'devices', force device_id mapping by (spoke_ip, hub_ip)
            if job.source == "devices" and job.device_ids:
                id_by_spoke_hub: dict[tuple[str, str], int] = {}
                for did in job.device_ids:
                    dev = db.get_device(did)
                    if dev:
                        id_by_spoke_hub[(dev["spoke_ip"], dev["hub_ip"])] = did
                for row in site_rows:
                    key = (row["spoke_ip"], row["hub_ip"])
                    if key in id_by_spoke_hub:
                        row["device_id"] = id_by_spoke_hub[key]
            db.insert_run_sites(job.id, site_rows)

            # Auto-update device name when test discovers a real hostname
            for row in site_rows:
                dname = row.get("display_name") or ""
                spoke = row.get("spoke_ip") or ""
                if not dname or dname == spoke:
                    continue  # no real hostname discovered
                did = row.get("device_id")
                if did is None:
                    matched = db.find_device_by_spoke_hub(spoke, row.get("hub_ip") or "")
                    if matched:
                        did = matched["id"]
                if did:
                    db.update_device_name_if_unset(did, dname, spoke)

        db.finalize_run(
            run_id=job.id,
            status=job.status,
            exit_code=job.exit_code,
            finished_at=job.finished_at,
            archive_filename=job.archive_filename,
            summary=job.captured_summary,
        )

        job.append_line("")
        job.new_line_event.set()
        global ACTIVE_JOB_ID
        with JOBS_LOCK:
            if ACTIVE_JOB_ID == job.id:
                ACTIVE_JOB_ID = None


# --- App ------------------------------------------------------------------

app = FastAPI(title="FortiGate Traffic Test Runner")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def _active_job_id() -> str | None:
    with JOBS_LOCK:
        return ACTIVE_JOB_ID


def _check_active() -> None:
    aid = _active_job_id()
    if aid is not None:
        job = JOBS.get(aid)
        if job and job.status == "running":
            raise HTTPException(status_code=409, detail=f"A run is already in progress (job {aid}).")


def _new_job(*, source: str, input_name: str, device_ids: list[int] | None = None) -> JobState:
    global ACTIVE_JOB_ID
    with JOBS_LOCK:
        _check_active()
        job_id = uuid.uuid4().hex[:12]
        job = JobState(id=job_id, source=source, input_name=input_name, device_ids=list(device_ids or []))
        JOBS[job_id] = job
        ACTIVE_JOB_ID = job_id
    return job


# --- Index / CSV-upload run ----------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    stats = db.dashboard_stats()

    device_pass = device_fail = device_untested = 0
    for d in stats["device_health"]:
        throughput = d["last_throughput"]
        if throughput is None:
            device_untested += 1
            continue
        accepted = engine.parse_speed_to_mbps(d.get("accepted_speed") or "")
        if accepted is None:
            spd = engine.parse_speed_to_mbps(d.get("speed") or "")
            accepted = round(spd * 0.90, 2) if spd is not None else None
        if accepted is None or throughput >= accepted:
            device_pass += 1
        else:
            device_fail += 1

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "defaults": {
                "hub_server_intf": engine.DEFAULT_HUB_SERVER_INTF,
                "spoke_client_intf": engine.DEFAULT_SPOKE_CLIENT_INTF,
                "traffictest_port": engine.DEFAULT_TRAFFICTEST_PORT,
                "traffictest_duration": engine.DEFAULT_TRAFFICTEST_DURATION_SECONDS,
                "hub_server_start_delay": engine.DEFAULT_HUB_SERVER_START_DELAY_SECONDS,
                "delay_seconds": engine.DEFAULT_DELAY_SECONDS,
            },
            "active_job_id": _active_job_id(),
            "stats": stats,
            "device_pass": device_pass,
            "device_fail": device_fail,
            "device_untested": device_untested,
        },
    )


@app.post("/run")
async def start_run(
    input_file: UploadFile,
    sshuser: str = Form(""),
    sshpw: str = Form(""),
    hub_ip: str = Form(""),
    hub_mgmt_ip: str = Form(""),
    hub_server_intf: str = Form(""),
    spoke_client_intf: str = Form(""),
    traffictest_port: str = Form(""),
    traffictest_duration: int = Form(engine.DEFAULT_TRAFFICTEST_DURATION_SECONDS),
    hub_server_start_delay: float = Form(engine.DEFAULT_HUB_SERVER_START_DELAY_SECONDS),
    delay_seconds: int = Form(0),
    timeout: int = Form(0),
    skip_hub_setup: bool = Form(False),
    dry_run: bool = Form(False),
):
    job = _new_job(source="csv", input_name=input_file.filename or "input")
    suffix = Path(input_file.filename or "input.csv").suffix or ".csv"
    upload_path = UPLOAD_DIR / f"{job.id}{suffix}"
    with upload_path.open("wb") as fh:
        shutil.copyfileobj(input_file.file, fh)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = UPLOAD_DIR / f"_render_{timestamp}_{job.id}.html"

    settings = {
        "sshuser": sshuser, "hub_ip": hub_ip, "hub_mgmt_ip": hub_mgmt_ip,
        "hub_server_intf": hub_server_intf, "spoke_client_intf": spoke_client_intf,
        "traffictest_port": traffictest_port, "traffictest_duration": traffictest_duration,
        "hub_server_start_delay": hub_server_start_delay, "delay_seconds": delay_seconds,
        "timeout": timeout, "skip_hub_setup": skip_hub_setup, "dry_run": dry_run,
    }
    argv = _build_argv(
        input_path=upload_path, output_path=output_path,
        sshuser=sshuser, hub_ip=hub_ip, hub_mgmt_ip=hub_mgmt_ip,
        hub_server_intf=hub_server_intf, spoke_client_intf=spoke_client_intf,
        traffictest_port=traffictest_port, traffictest_duration=traffictest_duration,
        hub_server_start_delay=hub_server_start_delay, delay_seconds=delay_seconds,
        timeout=timeout if timeout > 0 else None,
        skip_hub_setup=skip_hub_setup, dry_run=dry_run,
    )

    threading.Thread(
        target=_run_job, args=(job, argv, sshpw, upload_path, settings), daemon=True,
    ).start()
    return RedirectResponse(url=f"/run/{job.id}", status_code=303)


@app.get("/run/{job_id}", response_class=HTMLResponse)
def view_run(request: Request, job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    devices_for_run = []
    for did in job.device_ids:
        d = db.get_device(did)
        if d:
            devices_for_run.append(d)
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "job": job,
            "active_job_id": _active_job_id(),
            "devices_for_run": devices_for_run,
        },
    )


@app.get("/run/{job_id}/status")
def run_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    reports = []
    if job.status == "done" and job.archive_filename:
        for fmt in ("html", "xlsx", "pdf"):
            reports.append({"label": fmt.upper(), "url": f"/archive/run/{job.id}/render/{fmt}"})
    return JSONResponse({
        "id": job.id,
        "status": job.status,
        "exit_code": job.exit_code,
        "error_message": job.error_message,
        "started_at": job.started_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "archive_filename": job.archive_filename,
        "reports": reports,
    })


@app.get("/run/{job_id}/stream")
async def stream_run(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    async def event_gen():
        offset = 0
        while True:
            lines = job.snapshot_from(offset)
            if lines:
                for line in lines:
                    yield f"data: {line}\n\n"
                offset += len(lines)
            terminal = job.status in {"done", "error"} and offset >= len(job.log_lines)
            if terminal:
                yield (
                    "event: end\n"
                    f"data: {job.status}:{job.exit_code if job.exit_code is not None else ''}\n\n"
                )
                return
            job.new_line_event.clear()
            await asyncio.get_event_loop().run_in_executor(None, job.new_line_event.wait, 1.0)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# --- Devices --------------------------------------------------------------

@app.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, error: str = "", message: str = ""):
    devices = db.list_devices()
    return templates.TemplateResponse(
        request,
        "devices.html",
        {
            "devices": devices,
            "defaults": {
                "hub_server_intf": engine.DEFAULT_HUB_SERVER_INTF,
                "spoke_client_intf": engine.DEFAULT_SPOKE_CLIENT_INTF,
                "traffictest_port": engine.DEFAULT_TRAFFICTEST_PORT,
                "traffictest_duration": engine.DEFAULT_TRAFFICTEST_DURATION_SECONDS,
                "hub_server_start_delay": engine.DEFAULT_HUB_SERVER_START_DELAY_SECONDS,
            },
            "active_job_id": _active_job_id(),
            "error": error,
            "message": message,
        },
    )


@app.post("/devices/new")
async def device_create(
    name: str = Form(""),
    spoke_ip: str = Form(...),
    hub_ip: str = Form(...),
    hub_mgmt_ip: str = Form(""),
    speed: str = Form(""),
    accepted_speed: str = Form(""),
    server_intf: str = Form(""),
    client_intf: str = Form(""),
    traffictest_port: str = Form(""),
    circuit_id: str = Form(""),
    isp: str = Form(""),
    notes: str = Form(""),
):
    try:
        db.create_device({
            "name": name, "spoke_ip": spoke_ip, "hub_ip": hub_ip,
            "hub_mgmt_ip": hub_mgmt_ip, "speed": speed, "accepted_speed": accepted_speed,
            "server_intf": server_intf, "client_intf": client_intf,
            "traffictest_port": traffictest_port,
            "circuit_id": circuit_id, "isp": isp,
            "notes": notes,
        })
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/devices?error={exc}", status_code=303)
    return RedirectResponse(url="/devices?message=Device+added", status_code=303)


@app.get("/devices/{device_id}/edit", response_class=HTMLResponse)
def device_edit_form(request: Request, device_id: int):
    device = db.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    return templates.TemplateResponse(
        request,
        "device_edit.html",
        {"device": device, "active_job_id": _active_job_id()},
    )


@app.post("/devices/{device_id}/edit")
async def device_update(
    device_id: int,
    name: str = Form(""),
    spoke_ip: str = Form(...),
    hub_ip: str = Form(...),
    hub_mgmt_ip: str = Form(""),
    speed: str = Form(""),
    accepted_speed: str = Form(""),
    server_intf: str = Form(""),
    client_intf: str = Form(""),
    traffictest_port: str = Form(""),
    circuit_id: str = Form(""),
    isp: str = Form(""),
    notes: str = Form(""),
):
    db.update_device(device_id, {
        "name": name, "spoke_ip": spoke_ip, "hub_ip": hub_ip,
        "hub_mgmt_ip": hub_mgmt_ip, "speed": speed, "accepted_speed": accepted_speed,
        "server_intf": server_intf, "client_intf": client_intf,
        "traffictest_port": traffictest_port,
        "circuit_id": circuit_id, "isp": isp,
        "notes": notes,
    })
    return RedirectResponse(url="/devices?message=Device+updated", status_code=303)


@app.post("/devices/{device_id}/delete")
def device_delete(device_id: int):
    db.delete_device(device_id)
    return RedirectResponse(url="/devices?message=Device+deleted", status_code=303)


@app.post("/devices/import")
async def device_import(input_file: UploadFile):
    suffix = Path(input_file.filename or "import.csv").suffix.lower() or ".csv"
    tmp = UPLOAD_DIR / f"_import_{uuid.uuid4().hex[:8]}{suffix}"
    with tmp.open("wb") as fh:
        shutil.copyfileobj(input_file.file, fh)
    try:
        rows = engine.load_rows(tmp, None)
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return RedirectResponse(url=f"/devices?error=Import+failed:+{exc}", status_code=303)

    normalized = []
    for raw in rows:
        norm = {engine.sanitize_key(k): v for k, v in raw.items()}
        normalized.append({
            "name": engine.find_first_value(norm, engine.NAME_ALIASES) or "",
            "spoke_ip": engine.find_first_value(norm, engine.IP_ALIASES),
            "hub_ip": engine.find_first_value(norm, engine.HUB_IP_ALIASES),
            "hub_mgmt_ip": engine.find_first_value(norm, engine.HUB_MGMT_IP_ALIASES),
            "speed": engine.find_first_value(norm, engine.SPEED_ALIASES),
            "server_intf": engine.find_first_value(norm, engine.HUB_SERVER_INTF_ALIASES),
            "client_intf": engine.find_first_value(norm, engine.SPOKE_CLIENT_INTF_ALIASES),
            "traffictest_port": engine.find_first_value(norm, engine.TRAFFICTEST_PORT_ALIASES),
            "circuit_id": engine.find_first_value(norm, engine.CIRCUIT_ID_ALIASES),
            "isp": engine.find_first_value(norm, engine.ISP_ALIASES),
            "notes": "",
        })
    inserted, updated = db.upsert_devices_from_rows(normalized)
    tmp.unlink(missing_ok=True)
    return RedirectResponse(
        url=f"/devices?message=Imported+{inserted}+new,+{updated}+updated", status_code=303,
    )


def _start_run_for_devices(
    *,
    device_ids: list[int],
    sshuser: str,
    sshpw: str,
    overrides: dict[str, Any],
) -> tuple[bool, str, str | None]:
    """Shared entry used by HTTP devices_run and the scheduler. Returns (ok, message, run_id)."""
    if not device_ids:
        return False, "No devices selected.", None
    if _active_job_id() is not None:
        active = JOBS.get(_active_job_id() or "")
        if active and active.status == "running":
            return False, f"A run is already in progress (job {active.id}).", None

    try:
        job = _new_job(source="devices", input_name=f"{len(device_ids)} device(s)", device_ids=device_ids)
    except HTTPException as exc:
        return False, str(exc.detail), None

    upload_path = UPLOAD_DIR / f"{job.id}.csv"
    with upload_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "spoke_ip", "hub_ip", "hub_mgmt_ip", "speed",
            "server_intf", "client_intf", "traffictest_port",
            "circuit_id", "isp",
        ])
        for did in device_ids:
            d = db.get_device(did)
            if d is None:
                continue
            writer.writerow([
                d["spoke_ip"], d["hub_ip"], d["hub_mgmt_ip"], d["speed"],
                d["server_intf"], d["client_intf"], d["traffictest_port"],
                d.get("circuit_id") or "", d.get("isp") or "",
            ])

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = UPLOAD_DIR / f"_render_{timestamp}_{job.id}.html"

    o = overrides
    settings = {
        "sshuser": sshuser, "device_ids": list(device_ids), **o,
    }
    argv = _build_argv(
        input_path=upload_path, output_path=output_path,
        sshuser=sshuser,
        hub_ip=o.get("hub_ip", ""),
        hub_mgmt_ip=o.get("hub_mgmt_ip", ""),
        hub_server_intf=o.get("hub_server_intf", ""),
        spoke_client_intf=o.get("spoke_client_intf", ""),
        traffictest_port=o.get("traffictest_port", ""),
        traffictest_duration=int(o.get("traffictest_duration") or engine.DEFAULT_TRAFFICTEST_DURATION_SECONDS),
        hub_server_start_delay=float(o.get("hub_server_start_delay") or engine.DEFAULT_HUB_SERVER_START_DELAY_SECONDS),
        delay_seconds=int(o.get("delay_seconds") or 0),
        timeout=int(o["timeout"]) if int(o.get("timeout") or 0) > 0 else None,
        skip_hub_setup=bool(o.get("skip_hub_setup")),
        dry_run=bool(o.get("dry_run")),
    )

    threading.Thread(
        target=_run_job, args=(job, argv, sshpw, upload_path, settings), daemon=True,
    ).start()
    return True, f"started run {job.id}", job.id


@app.post("/devices/run")
async def devices_run(
    device_ids: list[int] = Form(...),
    sshuser: str = Form(""),
    sshpw: str = Form(""),
    hub_ip: str = Form(""),
    hub_mgmt_ip: str = Form(""),
    hub_server_intf: str = Form(""),
    spoke_client_intf: str = Form(""),
    traffictest_port: str = Form(""),
    traffictest_duration: int = Form(engine.DEFAULT_TRAFFICTEST_DURATION_SECONDS),
    hub_server_start_delay: float = Form(engine.DEFAULT_HUB_SERVER_START_DELAY_SECONDS),
    delay_seconds: int = Form(0),
    timeout: int = Form(0),
    skip_hub_setup: bool = Form(False),
    dry_run: bool = Form(False),
):
    overrides = {
        "hub_ip": hub_ip, "hub_mgmt_ip": hub_mgmt_ip,
        "hub_server_intf": hub_server_intf, "spoke_client_intf": spoke_client_intf,
        "traffictest_port": traffictest_port, "traffictest_duration": traffictest_duration,
        "hub_server_start_delay": hub_server_start_delay, "delay_seconds": delay_seconds,
        "timeout": timeout, "skip_hub_setup": skip_hub_setup, "dry_run": dry_run,
    }
    ok, message, run_id = _start_run_for_devices(
        device_ids=device_ids, sshuser=sshuser, sshpw=sshpw, overrides=overrides,
    )
    if not ok:
        raise HTTPException(status_code=409 if "in progress" in message.lower() else 400, detail=message)
    return RedirectResponse(url=f"/run/{run_id}", status_code=303)


# --- Archive --------------------------------------------------------------

@app.get("/archive", response_class=HTMLResponse)
def archive_page(request: Request):
    devices = db.list_devices()
    return templates.TemplateResponse(
        request,
        "archive.html",
        {"devices": devices, "active_job_id": _active_job_id()},
    )


@app.get("/archive/device/{device_id}", response_class=HTMLResponse)
def archive_device(request: Request, device_id: int):
    device = db.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    runs = db.runs_for_device(device_id)
    # Use accepted_speed if explicitly set, else fall back to 90% of speed (same as dashboard)
    target_mbps = engine.parse_speed_to_mbps(device.get("accepted_speed") or "")
    if target_mbps is None and device.get("speed"):
        spd = engine.parse_speed_to_mbps(device["speed"] or "")
        target_mbps = round(spd * 0.90, 2) if spd is not None else None
    chart_points = [
        {
            "run_id": r["id"],
            "started_at": r["started_at"],
            "throughput_mbps": r["throughput_mbps"],
            "passed": (
                r["throughput_mbps"] is not None
                and target_mbps is not None
                and r["throughput_mbps"] >= target_mbps
            ),
        }
        for r in runs
        if r["throughput_mbps"] is not None
    ]
    chart_points.reverse()  # oldest → newest for left-to-right plotting
    return templates.TemplateResponse(
        request,
        "device_archive.html",
        {
            "device": device,
            "runs": runs,
            "active_job_id": _active_job_id(),
            "target_mbps": target_mbps,
            "chart_points": chart_points,
        },
    )


@app.get("/archive/run/{run_id}/render/{fmt}")
def archive_render(run_id: str, fmt: str):
    fmt = fmt.lower()
    if fmt not in {"html", "xlsx", "pdf"}:
        raise HTTPException(status_code=400, detail="Unsupported format. Use html, xlsx, or pdf.")
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if not run["archive_filename"]:
        raise HTTPException(status_code=404, detail="No archive recorded for this run.")
    try:
        raw = ftp_archive.fetch_bytes(run["archive_filename"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to fetch archive from FTP: {exc}")
    payload = json.loads(raw.decode("utf-8"))
    runs_objs, summary, templates_list, delay = serialize.deserialize_runs(payload)
    input_path = Path(payload.get("input_name", "input.csv"))

    download_base = f"traffic_test_report_{run_id}"
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        if fmt == "html":
            html = engine.build_html_report(
                input_path=input_path,
                output_path=td_path / f"{download_base}.html",
                results=runs_objs,
                command_templates=templates_list,
                delay_seconds=delay,
            )
            return Response(
                content=html, media_type="text/html",
                headers={"Content-Disposition": f'inline; filename="{download_base}.html"'},
            )
        if fmt == "xlsx":
            out = td_path / f"{download_base}.xlsx"
            engine.build_excel_report(runs_objs, summary, out)
            data = out.read_bytes()
            return Response(
                content=data,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="{download_base}.xlsx"'},
            )
        # pdf
        out = td_path / f"{download_base}.pdf"
        engine.build_pdf_report(runs_objs, summary, out)
        if not out.exists():
            raise HTTPException(status_code=500, detail="PDF generation produced no file.")
        data = out.read_bytes()
        return Response(
            content=data, media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{download_base}.pdf"'},
        )


# --- Schedules ------------------------------------------------------------

_DAYS = [(1, "Mon"), (2, "Tue"), (3, "Wed"), (4, "Thu"), (5, "Fri"), (6, "Sat"), (7, "Sun")]
_MONTHS = [(i, calendar.month_name[i]) for i in range(1, 13)]


def _parse_schedule_form(form: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Validate and normalize a schedule form payload. Returns (values, error_or_None)."""
    name = (form.get("name") or "").strip()
    pattern = (form.get("pattern") or "").strip()
    if not name:
        return {}, "Name is required."
    if pattern not in {"once", "daily", "weekly", "monthly", "yearly"}:
        return {}, "Pattern must be once, daily, weekly, monthly, or yearly."

    raw_devices = form.get("device_ids") or []
    if isinstance(raw_devices, str):
        raw_devices = [raw_devices]
    try:
        device_ids = [int(x) for x in raw_devices if str(x).strip()]
    except ValueError:
        return {}, "Invalid device id in selection."
    if not device_ids:
        return {}, "Select at least one device."

    sshuser = (form.get("sshuser") or "").strip()
    sshpw = form.get("sshpw") or ""
    if not sshuser or not sshpw:
        return {}, "SSH username and password are required."

    days_of_week = ""
    day_of_month: int | None = None
    month_of_year: int | None = None
    if pattern == "once":
        run_time = (form.get("run_at") or "").strip()  # 'YYYY-MM-DDTHH:MM'
        if not run_time:
            return {}, "Date and time are required for 'once'."
    elif pattern == "daily":
        run_time = (form.get("time_of_day") or "").strip()
        if not run_time:
            return {}, "Time of day is required for 'daily'."
    elif pattern == "weekly":
        run_time = (form.get("time_of_day") or "").strip()
        if not run_time:
            return {}, "Time of day is required for 'weekly'."
        raw_days = form.get("days") or []
        if isinstance(raw_days, str):
            raw_days = [raw_days]
        try:
            days = sorted({int(d) for d in raw_days if 1 <= int(d) <= 7})
        except ValueError:
            return {}, "Invalid day-of-week value."
        if not days:
            return {}, "Select at least one day of the week."
        days_of_week = ",".join(str(d) for d in days)
    elif pattern == "monthly":
        run_time = (form.get("time_of_day") or "").strip()
        if not run_time:
            return {}, "Time of day is required for 'monthly'."
        try:
            day_of_month = int(form.get("day_of_month") or 0)
        except ValueError:
            return {}, "Day of month must be an integer."
        if not (1 <= day_of_month <= 31):
            return {}, "Day of month must be between 1 and 31."
    else:  # yearly
        run_time = (form.get("time_of_day") or "").strip()
        if not run_time:
            return {}, "Time of day is required for 'yearly'."
        try:
            month_of_year = int(form.get("month_of_year") or 0)
            day_of_month = int(form.get("day_of_month") or 0)
        except ValueError:
            return {}, "Month and day must be integers."
        if not (1 <= month_of_year <= 12):
            return {}, "Month must be between 1 and 12."
        if not (1 <= day_of_month <= 31):
            return {}, "Day of month must be between 1 and 31."

    overrides = {
        "hub_ip": form.get("hub_ip") or "",
        "hub_mgmt_ip": form.get("hub_mgmt_ip") or "",
        "hub_server_intf": form.get("hub_server_intf") or "",
        "spoke_client_intf": form.get("spoke_client_intf") or "",
        "traffictest_port": form.get("traffictest_port") or "",
        "traffictest_duration": int(form.get("traffictest_duration") or engine.DEFAULT_TRAFFICTEST_DURATION_SECONDS),
        "hub_server_start_delay": float(form.get("hub_server_start_delay") or engine.DEFAULT_HUB_SERVER_START_DELAY_SECONDS),
        "delay_seconds": int(form.get("delay_seconds") or 0),
        "timeout": int(form.get("timeout") or 0),
        "skip_hub_setup": bool(form.get("skip_hub_setup")),
        "dry_run": bool(form.get("dry_run")),
    }

    next_dt = scheduler.compute_next_run(
        pattern=pattern, run_time=run_time, days_of_week=days_of_week,
        day_of_month=day_of_month, month_of_year=month_of_year,
    )
    next_run_at = next_dt.isoformat(timespec="seconds") if next_dt else None

    return {
        "name": name,
        "enabled": form.get("enabled", True),
        "pattern": pattern,
        "run_time": run_time,
        "days_of_week": days_of_week,
        "day_of_month": day_of_month,
        "month_of_year": month_of_year,
        "device_ids": json.dumps(device_ids),
        "sshuser": sshuser,
        "sshpw": sshpw,
        "overrides_json": json.dumps(overrides),
        "next_run_at": next_run_at,
    }, None


@app.get("/schedules", response_class=HTMLResponse)
def schedules_page(request: Request, error: str = "", message: str = ""):
    schedules = db.list_schedules()
    # Decorate with device summaries
    for s in schedules:
        try:
            ids = json.loads(s["device_ids"])
        except Exception:  # noqa: BLE001
            ids = []
        names = []
        for did in ids:
            d = db.get_device(int(did))
            if d:
                names.append(d["name"] or d["spoke_ip"])
        s["device_summary"] = ", ".join(names) if names else f"{len(ids)} device(s)"
        if s["pattern"] == "weekly" and s["days_of_week"]:
            label_map = dict(_DAYS)
            try:
                s["days_label"] = ", ".join(label_map[int(d)] for d in s["days_of_week"].split(","))
            except (KeyError, ValueError):
                s["days_label"] = s["days_of_week"]
        else:
            s["days_label"] = ""
        s["month_name"] = calendar.month_name[s["month_of_year"]] if s.get("month_of_year") else ""
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "schedules": schedules,
            "active_job_id": _active_job_id(),
            "error": error, "message": message,
        },
    )


def _schedule_form_ctx(request: Request, schedule: dict | None, form: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
    """Build the template ctx for the schedule edit form, optionally pre-filled from a failed POST."""
    devices = db.list_devices()
    selected_ids: list[int] = []
    selected_days: list[int] = []
    overrides: dict[str, Any] = {}
    placeholder = None  # shape that matches `schedule` in template

    if form is not None:
        # Hydrate from POSTed values so the user doesn't lose input on error.
        raw_devices = form.get("device_ids") or []
        if isinstance(raw_devices, str):
            raw_devices = [raw_devices]
        selected_ids = [int(x) for x in raw_devices if str(x).strip().isdigit()]
        raw_days = form.get("days") or []
        if isinstance(raw_days, str):
            raw_days = [raw_days]
        selected_days = [int(x) for x in raw_days if str(x).strip().isdigit()]
        overrides = {
            "hub_ip": form.get("hub_ip") or "",
            "hub_mgmt_ip": form.get("hub_mgmt_ip") or "",
            "hub_server_intf": form.get("hub_server_intf") or "",
            "spoke_client_intf": form.get("spoke_client_intf") or "",
            "traffictest_port": form.get("traffictest_port") or "",
            "traffictest_duration": form.get("traffictest_duration") or engine.DEFAULT_TRAFFICTEST_DURATION_SECONDS,
            "hub_server_start_delay": form.get("hub_server_start_delay") or engine.DEFAULT_HUB_SERVER_START_DELAY_SECONDS,
            "delay_seconds": form.get("delay_seconds") or 0,
            "timeout": form.get("timeout") or 0,
            "skip_hub_setup": bool(form.get("skip_hub_setup")),
            "dry_run": bool(form.get("dry_run")),
        }
        run_time = form.get("run_at") if (form.get("pattern") == "once") else form.get("time_of_day")
        placeholder = {
            "id": schedule["id"] if schedule else 0,
            "name": form.get("name") or "",
            "pattern": form.get("pattern") or "daily",
            "run_time": run_time or "",
            "sshuser": form.get("sshuser") or "",
            "sshpw": form.get("sshpw") or "",
            "enabled": True,
            "day_of_month": int(form.get("day_of_month")) if str(form.get("day_of_month") or "").isdigit() else None,
            "month_of_year": int(form.get("month_of_year")) if str(form.get("month_of_year") or "").isdigit() else None,
        }
    elif schedule is not None:
        try:
            selected_ids = [int(x) for x in json.loads(schedule["device_ids"])]
        except Exception:  # noqa: BLE001
            selected_ids = []
        selected_days = [int(d) for d in (schedule["days_of_week"] or "").split(",") if d.strip()]
        overrides = json.loads(schedule["overrides_json"] or "{}")

    return {
        "request": request,
        "devices": devices,
        "schedule": placeholder or schedule,
        "selected_device_ids": selected_ids,
        "days_list": _DAYS,
        "selected_days": selected_days,
        "months_list": _MONTHS,
        "overrides": overrides,
        "error": error,
        "active_job_id": _active_job_id(),
        "defaults": {
            "hub_server_intf": engine.DEFAULT_HUB_SERVER_INTF,
            "spoke_client_intf": engine.DEFAULT_SPOKE_CLIENT_INTF,
            "traffictest_port": engine.DEFAULT_TRAFFICTEST_PORT,
            "traffictest_duration": engine.DEFAULT_TRAFFICTEST_DURATION_SECONDS,
            "hub_server_start_delay": engine.DEFAULT_HUB_SERVER_START_DELAY_SECONDS,
        },
    }


@app.get("/schedules/new", response_class=HTMLResponse)
def schedule_new_form(request: Request):
    return templates.TemplateResponse(
        request,
        "schedule_edit.html",
        _schedule_form_ctx(request, schedule=None),
    )


@app.get("/schedules/{schedule_id}/edit", response_class=HTMLResponse)
def schedule_edit_form(request: Request, schedule_id: int):
    s = db.get_schedule(schedule_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return templates.TemplateResponse(
        request,
        "schedule_edit.html",
        _schedule_form_ctx(request, schedule=s),
    )


@app.post("/schedules/new")
async def schedule_create(request: Request):
    form_data = await request.form()
    form = {k: form_data.getlist(k) if k in {"device_ids", "days"} else form_data.get(k) for k in form_data.keys()}
    values, err = _parse_schedule_form(form)
    if err:
        return templates.TemplateResponse(
            request,
            "schedule_edit.html",
            _schedule_form_ctx(request, schedule=None, form=form, error=err),
            status_code=400,
        )
    db.create_schedule(values)
    return RedirectResponse(url="/schedules?message=Schedule+created", status_code=303)


@app.post("/schedules/{schedule_id}/edit")
async def schedule_update(schedule_id: int, request: Request):
    form_data = await request.form()
    form = {k: form_data.getlist(k) if k in {"device_ids", "days"} else form_data.get(k) for k in form_data.keys()}
    values, err = _parse_schedule_form(form)
    if err:
        existing = db.get_schedule(schedule_id)
        return templates.TemplateResponse(
            request,
            "schedule_edit.html",
            _schedule_form_ctx(request, schedule=existing, form=form, error=err),
            status_code=400,
        )
    db.update_schedule(schedule_id, values)
    return RedirectResponse(url="/schedules?message=Schedule+updated", status_code=303)


@app.post("/schedules/{schedule_id}/delete")
def schedule_delete(schedule_id: int):
    db.delete_schedule(schedule_id)
    return RedirectResponse(url="/schedules?message=Schedule+deleted", status_code=303)


@app.post("/schedules/{schedule_id}/toggle")
def schedule_toggle(schedule_id: int):
    s = db.get_schedule(schedule_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    new_enabled = not bool(s["enabled"])
    next_at = None
    if new_enabled:
        next_dt = scheduler.compute_next_run(
            pattern=s["pattern"], run_time=s["run_time"], days_of_week=s["days_of_week"],
            day_of_month=s.get("day_of_month"), month_of_year=s.get("month_of_year"),
        )
        next_at = next_dt.isoformat(timespec="seconds") if next_dt else None
    db.set_schedule_enabled(schedule_id, new_enabled, next_at)
    return RedirectResponse(url="/schedules", status_code=303)


@app.post("/schedules/{schedule_id}/run")
def schedule_run_now(schedule_id: int):
    s = db.get_schedule(schedule_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    try:
        device_ids = [int(x) for x in json.loads(s["device_ids"])]
        overrides = json.loads(s["overrides_json"] or "{}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid schedule payload: {exc}")
    ok, message, run_id = _start_run_for_devices(
        device_ids=device_ids, sshuser=s["sshuser"], sshpw=s["sshpw"], overrides=overrides,
    )
    if not ok:
        raise HTTPException(status_code=409 if "in progress" in message.lower() else 400, detail=message)
    return RedirectResponse(url=f"/run/{run_id}", status_code=303)


# --- Health ---------------------------------------------------------------

@app.get("/healthz")
def healthz():
    ftp_ok, ftp_detail = ftp_archive.ping()
    return {"ok": True, "ftp": {"ok": ftp_ok, "detail": ftp_detail}}


# --- Startup --------------------------------------------------------------

@app.on_event("startup")
def _startup_scheduler() -> None:
    scheduler.start(
        start_run_callable=lambda *, device_ids, sshuser, sshpw, overrides:
            _start_run_for_devices(
                device_ids=device_ids, sshuser=sshuser, sshpw=sshpw, overrides=overrides,
            )
    )
