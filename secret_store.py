"""Encrypt-at-rest helpers for SSH credentials.

The key is resolved in this order:
  1. IPERF_SECRET_KEY env var (urlsafe-base64 Fernet key, 44 chars).
  2. File at IPERF_SECRET_KEY_FILE.
  3. ${IPERF_DATA_DIR}/.secret.key (auto-created with mode 0600).
  4. <repo>/data/.secret.key (CLI default, also auto-created).

Plaintext values that the encryption layer sees during migration are
returned as-is by `decrypt`, so old rows stay readable until the next
write re-encrypts them.
"""
from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "cryptography is required for credential encryption.\n"
        "Install it with: pip install cryptography"
    ) from exc

_REPO_ROOT = Path(__file__).resolve().parent
_LOCK = threading.Lock()
_KEY: bytes | None = None
_FERNET: Fernet | None = None


def _default_key_path() -> Path:
    data_dir = os.environ.get("IPERF_DATA_DIR")
    base = Path(data_dir).resolve() if data_dir else (_REPO_ROOT / "data")
    return base / ".secret.key"


def _read_key_from_file(path: Path) -> bytes | None:
    try:
        raw = path.read_bytes().strip()
    except FileNotFoundError:
        return None
    return raw or None


def _create_key_file(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Write with 0600 (POSIX). On Windows this is best-effort.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _load_key() -> bytes:
    env_key = os.environ.get("IPERF_SECRET_KEY", "").strip()
    if env_key:
        return env_key.encode("ascii")

    env_path = os.environ.get("IPERF_SECRET_KEY_FILE", "").strip()
    if env_path:
        key = _read_key_from_file(Path(env_path))
        if key:
            return key

    default_path = _default_key_path()
    key = _read_key_from_file(default_path)
    if key:
        return key
    return _create_key_file(default_path)


def _fernet() -> Fernet:
    global _KEY, _FERNET
    with _LOCK:
        if _FERNET is None:
            _KEY = _load_key()
            _FERNET = Fernet(_KEY)
        return _FERNET


_FERNET_PREFIX = b"gAAAAA"


def is_fernet_token(value: str | bytes) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        value = value.encode("ascii", errors="ignore")
    return value.startswith(_FERNET_PREFIX)


def encrypt(plain: str) -> str:
    """Encrypt a plaintext credential. Empty strings are returned as-is."""
    if not plain:
        return ""
    if is_fernet_token(plain):
        return plain  # already encrypted, don't double-wrap
    token = _fernet().encrypt(plain.encode("utf-8"))
    return token.decode("ascii")


def decrypt(value: str) -> str:
    """Decrypt a Fernet token. Plaintext (legacy) values pass through unchanged."""
    if not value:
        return ""
    if not is_fernet_token(value):
        return value
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return value


def random_token(nbytes: int = 16) -> str:
    """Convenience for callers that want a random secret unrelated to credentials."""
    return secrets.token_urlsafe(nbytes)
