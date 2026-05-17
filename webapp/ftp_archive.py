from __future__ import annotations

import io
import os
import time
from ftplib import FTP, error_perm


_RETRY_ATTEMPTS = 3
_RETRY_DELAY = 0.5


def _client() -> FTP:
    host = os.environ.get("FTP_HOST", "ftp-archive")
    port = int(os.environ.get("FTP_PORT", "21"))
    user = os.environ.get("FTP_USER", "archive")
    passwd = os.environ.get("FTP_PASS", "archive")
    timeout = float(os.environ.get("FTP_TIMEOUT", "20"))
    ftp = FTP(timeout=timeout)
    ftp.connect(host, port)
    ftp.login(user, passwd)
    ftp.set_pasv(True)
    return ftp


def _with_retry(fn):
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return fn()
        except (OSError, EOFError) as exc:
            last_exc = exc
            if attempt + 1 < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_DELAY * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def push_bytes(filename: str, data: bytes) -> None:
    def _do() -> None:
        with _client() as ftp:
            bio = io.BytesIO(data)
            ftp.storbinary(f"STOR {filename}", bio)
    _with_retry(_do)


def fetch_bytes(filename: str) -> bytes:
    def _do() -> bytes:
        buf = bytearray()
        with _client() as ftp:
            ftp.retrbinary(f"RETR {filename}", buf.extend)
        return bytes(buf)
    return _with_retry(_do)


def list_files() -> list[str]:
    with _client() as ftp:
        try:
            return sorted(ftp.nlst())
        except error_perm:
            return []


def exists(filename: str) -> bool:
    with _client() as ftp:
        try:
            ftp.size(filename)
            return True
        except error_perm:
            return False


def ping() -> tuple[bool, str]:
    try:
        with _client() as ftp:
            ftp.voidcmd("NOOP")
            return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
