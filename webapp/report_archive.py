"""Report archive backed by the local filesystem (PV mount in K8s, bind volume locally)."""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _reports_dir() -> Path:
    """Return the reports directory, creating it if needed.

    Resolves to ``$IPERF_DATA_DIR/reports`` (mounted as a PV in K8s) or
    ``<repo>/data/reports`` for local development.
    """
    base = Path(os.environ.get("IPERF_DATA_DIR", str(_ROOT / "data")))
    d = base / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_path(filename: str) -> Path:
    """Resolve a report filename under the reports dir, rejecting path traversal.

    Defense-in-depth: filenames are currently server-generated, but guard against
    any future caller passing a value containing ``/`` or ``..``.
    """
    name = (filename or "").strip()
    if not name or name != Path(name).name or name in (".", ".."):
        raise ValueError(f"Unsafe report filename: {filename!r}")
    return _reports_dir() / name


def push_bytes(filename: str, data: bytes) -> None:
    _safe_path(filename).write_bytes(data)


def fetch_bytes(filename: str) -> bytes:
    return _safe_path(filename).read_bytes()


def list_files() -> list[str]:
    d = _reports_dir()
    return sorted(p.name for p in d.iterdir() if p.is_file())


def exists(filename: str) -> bool:
    try:
        return _safe_path(filename).exists()
    except ValueError:
        return False


def ping() -> tuple[bool, str]:
    try:
        d = _reports_dir()
        return True, str(d)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
