from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import queue
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
for d in (UPLOAD_DIR,):
    d.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT))
import main as engine  # noqa: E402

from webapp import db, ftp_archive, serialize  # noqa: E402

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
    queue: queue.Queue = field(default_factory=queue.Queue)
    thread: threading.Thread | None = None
    input_name: str = ""
    source: str = "csv"
    archive_filename: str | None = None
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    captured_runs: Any = None  # filled by the hook
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
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "defaults": {
                "hub_server_intf": engine.DEFAULT_HUB_SERVER_INTF,
                "spoke_client_intf": engine.DEFAULT_SPOKE_CLIENT_INTF,
                "traffictest_port": engine.DEFAULT_TRAFFICTEST_PORT,
                "traffictest_duration": engine.DEFAULT_TRAFFICTEST_DURATION_SECONDS,
                "hub_server_start_delay": engine.DEFAULT_HUB_SERVER_START_DELAY_SECONDS,
                "delay_seconds": engine.DEFAULT_DELAY_SECONDS,
            },
            "active_job_id": _active_job_id(),
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

    thread = threading.Thread(
        target=_run_job, args=(job, argv, sshpw, upload_path, settings), daemon=True,
    )
    job.thread = thread
    thread.start()
    return RedirectResponse(url=f"/run/{job.id}", status_code=303)


@app.get("/run/{job_id}", response_class=HTMLResponse)
def view_run(request: Request, job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return templates.TemplateResponse("run.html", {"request": request, "job": job})


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
        "devices.html",
        {
            "request": request,
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
    server_intf: str = Form(""),
    client_intf: str = Form(""),
    traffictest_port: str = Form(""),
    notes: str = Form(""),
):
    try:
        db.create_device({
            "name": name, "spoke_ip": spoke_ip, "hub_ip": hub_ip,
            "hub_mgmt_ip": hub_mgmt_ip, "speed": speed,
            "server_intf": server_intf, "client_intf": client_intf,
            "traffictest_port": traffictest_port, "notes": notes,
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
        "device_edit.html",
        {"request": request, "device": device, "active_job_id": _active_job_id()},
    )


@app.post("/devices/{device_id}/edit")
async def device_update(
    device_id: int,
    name: str = Form(""),
    spoke_ip: str = Form(...),
    hub_ip: str = Form(...),
    hub_mgmt_ip: str = Form(""),
    speed: str = Form(""),
    server_intf: str = Form(""),
    client_intf: str = Form(""),
    traffictest_port: str = Form(""),
    notes: str = Form(""),
):
    db.update_device(device_id, {
        "name": name, "spoke_ip": spoke_ip, "hub_ip": hub_ip,
        "hub_mgmt_ip": hub_mgmt_ip, "speed": speed,
        "server_intf": server_intf, "client_intf": client_intf,
        "traffictest_port": traffictest_port, "notes": notes,
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
            "notes": "",
        })
    inserted, updated = db.upsert_devices_from_rows(normalized)
    tmp.unlink(missing_ok=True)
    return RedirectResponse(
        url=f"/devices?message=Imported+{inserted}+new,+{updated}+updated", status_code=303,
    )


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
    if not device_ids:
        raise HTTPException(status_code=400, detail="No devices selected.")
    job = _new_job(source="devices", input_name=f"{len(device_ids)} device(s)", device_ids=device_ids)

    # Synthesize a CSV file from the selected devices so we can reuse the engine.
    upload_path = UPLOAD_DIR / f"{job.id}.csv"
    with upload_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "spoke_ip", "hub_ip", "hub_mgmt_ip", "speed",
            "server_intf", "client_intf", "traffictest_port",
        ])
        for did in device_ids:
            d = db.get_device(did)
            if d is None:
                continue
            writer.writerow([
                d["spoke_ip"], d["hub_ip"], d["hub_mgmt_ip"], d["speed"],
                d["server_intf"], d["client_intf"], d["traffictest_port"],
            ])

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = UPLOAD_DIR / f"_render_{timestamp}_{job.id}.html"

    settings = {
        "sshuser": sshuser, "hub_ip": hub_ip, "hub_mgmt_ip": hub_mgmt_ip,
        "hub_server_intf": hub_server_intf, "spoke_client_intf": spoke_client_intf,
        "traffictest_port": traffictest_port, "traffictest_duration": traffictest_duration,
        "hub_server_start_delay": hub_server_start_delay, "delay_seconds": delay_seconds,
        "timeout": timeout, "skip_hub_setup": skip_hub_setup, "dry_run": dry_run,
        "device_ids": list(device_ids),
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

    thread = threading.Thread(
        target=_run_job, args=(job, argv, sshpw, upload_path, settings), daemon=True,
    )
    job.thread = thread
    thread.start()
    return RedirectResponse(url=f"/run/{job.id}", status_code=303)


# --- Archive --------------------------------------------------------------

@app.get("/archive", response_class=HTMLResponse)
def archive_page(request: Request):
    devices = db.list_devices()
    return templates.TemplateResponse(
        "archive.html",
        {"request": request, "devices": devices, "active_job_id": _active_job_id()},
    )


@app.get("/archive/device/{device_id}", response_class=HTMLResponse)
def archive_device(request: Request, device_id: int):
    device = db.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    runs = db.runs_for_device(device_id)
    target_mbps = engine.parse_speed_to_mbps(device["speed"] or "") if device.get("speed") else None
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
        "device_archive.html",
        {
            "request": request,
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


# --- Health ---------------------------------------------------------------

@app.get("/healthz")
def healthz():
    ftp_ok, ftp_detail = ftp_archive.ping()
    return {"ok": True, "ftp": {"ok": ftp_ok, "detail": ftp_detail}}
