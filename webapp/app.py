from __future__ import annotations

import asyncio
import queue
import shlex
import shutil
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__import__("os").environ.get("IPERF_DATA_DIR", str(ROOT / "data"))).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
REPORTS_DIR = DATA_DIR / "reports"
COMMANDS_DIR = DATA_DIR / "commands"
for d in (UPLOAD_DIR, REPORTS_DIR, COMMANDS_DIR):
    d.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT))
import main as engine  # noqa: E402  -- the CLI engine, reused as-is

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@dataclass
class JobState:
    id: str
    status: str = "pending"  # pending | running | done | error
    exit_code: int | None = None
    error_message: str | None = None
    log_lines: list[str] = field(default_factory=list)
    log_lock: threading.Lock = field(default_factory=threading.Lock)
    new_line_event: threading.Event = field(default_factory=threading.Event)
    queue: queue.Queue = field(default_factory=queue.Queue)
    thread: threading.Thread | None = None
    input_name: str = ""
    report_basename: str | None = None
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None

    def append_line(self, line: str) -> None:
        with self.log_lock:
            self.log_lines.append(line)
        self.new_line_event.set()

    def snapshot_from(self, offset: int) -> list[str]:
        with self.log_lock:
            return self.log_lines[offset:]


JOBS: dict[str, JobState] = {}
JOBS_LOCK = threading.Lock()
ACTIVE_JOB_ID: str | None = None


class _QueueTee:
    """Forwards writes to the original stream and to the job log line-buffered."""

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


def _build_argv(
    *,
    input_path: Path,
    output_path: Path,
    sheet: str,
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
    command_file: Path | None,
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
    if sheet:
        argv += ["--sheet", sheet]
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
    if command_file is not None:
        argv += ["--command-file", str(command_file)]
    return argv


def _run_job(job: JobState, argv: list[str], password: str) -> None:
    parser = engine.build_argument_parser()
    args = parser.parse_args(argv)
    # The CLI uses nargs="?" for --sshpw; here we always pass the literal string.
    args.sshpw = password if password else None

    original_stdout, original_stderr = sys.stdout, sys.stderr
    tee_out = _QueueTee(original_stdout, job)
    tee_err = _QueueTee(original_stderr, job)
    sys.stdout = tee_out
    sys.stderr = tee_err

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
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        job.finished_at = datetime.now()
        job.append_line("")  # flush trailing newline
        job.new_line_event.set()
        global ACTIVE_JOB_ID
        with JOBS_LOCK:
            if ACTIVE_JOB_ID == job.id:
                ACTIVE_JOB_ID = None


app = FastAPI(title="FortiGate Traffic Test Runner")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    global ACTIVE_JOB_ID
    with JOBS_LOCK:
        active_id = ACTIVE_JOB_ID
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
            "active_job_id": active_id,
        },
    )


@app.post("/run")
async def start_run(
    input_file: UploadFile,
    sheet: str = Form(""),
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
    commands: str = Form(""),
):
    global ACTIVE_JOB_ID
    with JOBS_LOCK:
        if ACTIVE_JOB_ID is not None:
            existing = JOBS.get(ACTIVE_JOB_ID)
            if existing and existing.status == "running":
                raise HTTPException(
                    status_code=409,
                    detail=f"A run is already in progress (job {ACTIVE_JOB_ID}).",
                )
        job_id = uuid.uuid4().hex[:12]
        job = JobState(id=job_id, input_name=input_file.filename or "input")
        JOBS[job_id] = job
        ACTIVE_JOB_ID = job_id

    suffix = Path(input_file.filename or "input.csv").suffix or ".csv"
    upload_path = UPLOAD_DIR / f"{job_id}{suffix}"
    with upload_path.open("wb") as fh:
        shutil.copyfileobj(input_file.file, fh)

    command_file_path: Path | None = None
    cleaned_commands = [ln.strip() for ln in commands.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if cleaned_commands:
        command_file_path = COMMANDS_DIR / f"{job_id}.txt"
        command_file_path.write_text("\n".join(cleaned_commands) + "\n", encoding="utf-8")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_basename = f"traffic_test_report_{timestamp}_{job_id}"
    output_path = REPORTS_DIR / f"{report_basename}.html"
    job.report_basename = report_basename

    argv = _build_argv(
        input_path=upload_path,
        output_path=output_path,
        sheet=sheet,
        sshuser=sshuser,
        hub_ip=hub_ip,
        hub_mgmt_ip=hub_mgmt_ip,
        hub_server_intf=hub_server_intf,
        spoke_client_intf=spoke_client_intf,
        traffictest_port=traffictest_port,
        traffictest_duration=traffictest_duration,
        hub_server_start_delay=hub_server_start_delay,
        delay_seconds=delay_seconds,
        timeout=timeout if timeout > 0 else None,
        skip_hub_setup=skip_hub_setup,
        dry_run=dry_run,
        command_file=command_file_path,
    )

    thread = threading.Thread(target=_run_job, args=(job, argv, sshpw), daemon=True)
    job.thread = thread
    thread.start()

    return RedirectResponse(url=f"/run/{job_id}", status_code=303)


@app.get("/run/{job_id}", response_class=HTMLResponse)
def view_run(request: Request, job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return templates.TemplateResponse(
        "run.html",
        {"request": request, "job": job},
    )


@app.get("/run/{job_id}/status")
def run_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    report_links = []
    if job.status == "done" and job.report_basename:
        for ext in ("html", "xlsx", "pdf"):
            path = REPORTS_DIR / f"{job.report_basename}.{ext}"
            if path.exists():
                report_links.append({"label": ext.upper(), "url": f"/reports/{path.name}"})
    return JSONResponse(
        {
            "id": job.id,
            "status": job.status,
            "exit_code": job.exit_code,
            "error_message": job.error_message,
            "started_at": job.started_at.isoformat(),
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "report_basename": job.report_basename,
            "reports": report_links,
        }
    )


@app.get("/run/{job_id}/stream")
async def stream_run(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    async def event_gen():
        offset = 0
        # Initial replay of any buffered lines.
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
            # Wait off the event loop so we don't block other requests.
            await asyncio.get_event_loop().run_in_executor(
                None, job.new_line_event.wait, 1.0
            )

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/reports", response_class=HTMLResponse)
def list_reports(request: Request):
    reports = []
    for path in sorted(REPORTS_DIR.glob("*.html"), reverse=True):
        stat = path.stat()
        base = path.stem
        siblings = []
        for ext in ("html", "xlsx", "pdf"):
            sib = REPORTS_DIR / f"{base}.{ext}"
            if sib.exists():
                siblings.append({"ext": ext, "url": f"/reports/{sib.name}", "size": sib.stat().st_size})
        reports.append(
            {
                "name": base,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "files": siblings,
            }
        )
    return templates.TemplateResponse("reports.html", {"request": request, "reports": reports})


@app.get("/reports/{filename}")
def serve_report(filename: str):
    safe = Path(filename).name  # strip path components
    path = REPORTS_DIR / safe
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Report not found.")
    media = "text/html" if safe.endswith(".html") else "application/octet-stream"
    return FileResponse(path, media_type=media, filename=safe if not safe.endswith(".html") else None)


@app.get("/healthz")
def healthz():
    return {"ok": True}
