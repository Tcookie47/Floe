"""Logging setup, secret redaction, and diagnostics bundle (SPEC §10).

No Qt here: `floe.core` must be usable headless. The UI is responsible only
for putting `build_diagnostics()`'s output on the clipboard.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from floe.core import paths

_REDACTED = "***"
_MIN_SECRET_LEN = 4

_lock = threading.Lock()
_secrets: set[str] = set()

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*")
_ACCOUNT_KEY_RE = re.compile(r"AccountKey=[^;\s]+")
_CLIENT_SECRET_RE = re.compile(r"client_secret=[^&\s]+")
# Three base64url-ish segments separated by dots, header starting "eyJ".
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

_setup_lock = threading.Lock()
_configured_log_file: Path | None = None


def register_secret(value: str | None) -> None:
    """Register a secret value so `redact()` masks every occurrence of it."""
    if not value or len(value) < _MIN_SECRET_LEN:
        return
    with _lock:
        _secrets.add(value)


def clear_secrets() -> None:
    """Forget all registered secret values (used in tests)."""
    with _lock:
        _secrets.clear()


def redact(text: str) -> str:
    """Mask secrets and known sensitive patterns in `text`."""
    if not text:
        return text

    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)

    for secret in secrets:
        if secret and len(secret) >= _MIN_SECRET_LEN:
            text = text.replace(secret, _REDACTED)

    text = _BEARER_RE.sub(f"Bearer {_REDACTED}", text)
    text = _ACCOUNT_KEY_RE.sub(f"AccountKey={_REDACTED}", text)
    text = _CLIENT_SECRET_RE.sub(f"client_secret={_REDACTED}", text)
    text = _JWT_RE.sub(_REDACTED, text)
    return text


class RedactionFilter(logging.Filter):
    """A logging filter that redacts the formatted message and exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None

        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            if exc_value is not None:
                redacted_exc_str = redact(str(exc_value))
                if redacted_exc_str != str(exc_value):
                    # Rebuild a lightweight exception carrying the redacted text so
                    # that default exception formatting doesn't leak the original.
                    record.exc_info = (exc_type, exc_type(redacted_exc_str), exc_tb)
        return True


_LOG_FILE_MODE = 0o600
_LOG_DIR_MODE = 0o700


def _restrict(path: Path, mode: int) -> None:
    """Best-effort chmod (a no-op for the group/other bits on Windows)."""
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


class PrivateRotatingFileHandler(RotatingFileHandler):
    """A `RotatingFileHandler` whose log files are readable by the owner only (0600).

    `_open` runs for the first file and again after every rollover. The process umask
    is tightened while the file is created (so it is never briefly world-readable),
    then the file is chmod-ed (which also fixes a file created by an older version)."""

    def _open(self):  # type: ignore[override]
        old_umask = os.umask(0o077)
        try:
            stream = super()._open()
        finally:
            os.umask(old_umask)
        _restrict(Path(self.baseFilename), _LOG_FILE_MODE)
        return stream


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the "floe" logger with a rotating, redacted file handler.

    Idempotent: calling it again with the same target log file does not add
    duplicate handlers. If the target log file changes (e.g. `FLOE_HOME`
    moved, as happens between tests), the handler is rebuilt to point at it.
    """
    global _configured_log_file
    logger = logging.getLogger("floe")

    with _setup_lock:
        logger.setLevel(level)

        log_dir = paths.logs_dir()
        log_file = log_dir / "app.log"

        if _configured_log_file != log_file:
            for old_handler in list(logger.handlers):
                logger.removeHandler(old_handler)
                old_handler.close()

            log_dir.mkdir(mode=_LOG_DIR_MODE, parents=True, exist_ok=True)
            _restrict(log_dir, _LOG_DIR_MODE)
            for old_file in log_dir.glob("app.log*"):
                _restrict(old_file, _LOG_FILE_MODE)  # rotated backups from older versions
            handler = PrivateRotatingFileHandler(
                log_file,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            formatter = logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            _configured_log_file = log_file

        for handler in logger.handlers:
            if not any(isinstance(f, RedactionFilter) for f in handler.filters):
                handler.addFilter(RedactionFilter())

    return logger


def recent_log_lines(n: int = 200) -> list[str]:
    """Return the last `n` lines of the log file, redacted."""
    log_file = paths.logs_dir() / "app.log"
    if not log_file.exists():
        return []
    with log_file.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    tail = lines[-n:] if n > 0 else []
    return [redact(line.rstrip("\n")) for line in tail]


def build_diagnostics(
    profile,
    catalog_status: str,
    version: str,
    commit: str,
) -> str:
    """Build the "Copy diagnostics" text bundle (SPEC §10).

    `profile` may be None (no active profile) or a `Profile`-like object
    exposing `to_dict()`; only its non-secret fields are included.
    """
    lines: list[str] = []
    lines.append(f"Floe version: {version} ({commit})")
    lines.append(f"Platform: {platform.platform()}")
    lines.append(f"Machine: {platform.machine()}")
    frozen = bool(getattr(sys, "frozen", False))
    lines.append(f"Python: {platform.python_version()}; frozen: {frozen}")
    try:
        import duckdb

        from floe.core.extensions import bundled_extension_dir

        lines.append(f"DuckDB: {duckdb.__version__}; bundled extensions: {bundled_extension_dir()}")
    except Exception as exc:  # pragma: no cover - diagnostics must never fail
        lines.append(f"DuckDB: unavailable ({type(exc).__name__})")
    lines.append("")

    lines.append("Active profile (non-secret fields):")
    if profile is None:
        lines.append("  (none)")
    else:
        profile_dict = profile.to_dict() if hasattr(profile, "to_dict") else dict(profile)
        for key in sorted(profile_dict):
            lines.append(f"  {key}: {profile_dict[key]!r}")
    lines.append("")

    lines.append(f"Catalog status: {catalog_status}")
    lines.append("")

    lines.append("Last log lines:")
    for line in recent_log_lines(200):
        lines.append(f"  {line}")

    return redact("\n".join(lines))
