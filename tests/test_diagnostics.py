"""Tests for floe.core.diagnostics (SPEC §10)."""

import logging
import os
import stat
import sys
from logging.handlers import RotatingFileHandler

import pytest

from floe.core import diagnostics, paths


def test_redact_masks_registered_secret():
    diagnostics.clear_secrets()
    try:
        diagnostics.register_secret("sk-super-secret-value-1234")
        text = "the key is sk-super-secret-value-1234 in the request"
        assert "sk-super-secret-value-1234" not in diagnostics.redact(text)
        assert "***" in diagnostics.redact(text)
    finally:
        diagnostics.clear_secrets()


def test_redact_ignores_short_or_empty_secrets():
    diagnostics.clear_secrets()
    try:
        diagnostics.register_secret("")
        diagnostics.register_secret("ab")
        text = "ab is a short string that should not be mangled"
        assert diagnostics.redact(text) == text
    finally:
        diagnostics.clear_secrets()


def test_redact_longest_match_first():
    diagnostics.clear_secrets()
    try:
        diagnostics.register_secret("abcd")
        diagnostics.register_secret("abcdefgh")
        text = "value=abcdefgh"
        redacted = diagnostics.redact(text)
        assert "abcdefgh" not in redacted
        assert "abcd" not in redacted
        assert redacted == "value=***"
    finally:
        diagnostics.clear_secrets()


def test_redact_bearer_header():
    text = "Authorization: Bearer abc123.def456-token"
    redacted = diagnostics.redact(text)
    assert "abc123.def456-token" not in redacted
    assert "Bearer ***" in redacted


def test_redact_account_key():
    text = (
        "DefaultEndpointsProtocol=https;AccountName=acct;"
        "AccountKey=SGVsbG8gV29ybGQ=;EndpointSuffix=x"
    )
    redacted = diagnostics.redact(text)
    assert "SGVsbG8gV29ybGQ=" not in redacted
    assert "AccountKey=***" in redacted


def test_redact_client_secret_query_and_form_style():
    form_text = "client_secret=abcDEF123&grant_type=client_credentials"
    query_text = "https://example.test/token?client_secret=abcDEF123&scope=x"
    for text in (form_text, query_text):
        redacted = diagnostics.redact(text)
        assert "abcDEF123" not in redacted
        assert "client_secret=***" in redacted


def test_redact_jwt_shaped_string():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQ_signature"
    text = f"token={jwt}"
    redacted = diagnostics.redact(text)
    assert jwt not in redacted
    assert "***" in redacted


def test_setup_logging_redacts_records(tmp_path):
    logger = diagnostics.setup_logging(level=logging.INFO)
    diagnostics.clear_secrets()
    try:
        diagnostics.register_secret("very-secret-value-999")
        logger.info("using secret %s in request", "very-secret-value-999")

        log_file = paths.logs_dir() / "app.log"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "very-secret-value-999" not in content
        assert "***" in content
    finally:
        diagnostics.clear_secrets()


def test_setup_logging_redacts_exception_text():
    logger = diagnostics.setup_logging(level=logging.INFO)
    diagnostics.clear_secrets()
    try:
        secret = "boom-secret-abcdef"
        diagnostics.register_secret(secret)
        try:
            raise ValueError("failed with " + secret + " in it")
        except ValueError:
            logger.exception("query failed")

        log_file = paths.logs_dir() / "app.log"
        content = log_file.read_text(encoding="utf-8")
        assert "boom-secret-abcdef" not in content
    finally:
        diagnostics.clear_secrets()


def test_setup_logging_is_idempotent():
    logger1 = diagnostics.setup_logging()
    handler_count = len(logger1.handlers)
    logger2 = diagnostics.setup_logging()
    assert logger1 is logger2
    assert len(logger2.handlers) == handler_count


def test_recent_log_lines_redacted(tmp_path):
    diagnostics.setup_logging()
    logger = logging.getLogger("floe")
    diagnostics.clear_secrets()
    try:
        diagnostics.register_secret("tail-secret-zzz999")
        logger.info("line with tail-secret-zzz999 inside")
        lines = diagnostics.recent_log_lines(200)
        assert any("line with" in line for line in lines)
        assert not any("tail-secret-zzz999" in line for line in lines)
    finally:
        diagnostics.clear_secrets()


def test_build_diagnostics_excludes_secrets():
    diagnostics.setup_logging()
    diagnostics.clear_secrets()
    try:
        diagnostics.register_secret("diag-secret-value-42")
        logger = logging.getLogger("floe")
        logger.info("connecting with diag-secret-value-42 present")

        class FakeProfile:
            def to_dict(self):
                return {"name": "profile-a", "adls_account": "acct-a"}

        text = diagnostics.build_diagnostics(
            profile=FakeProfile(),
            catalog_status="ok",
            version="0.0.0-test",
            commit="abc1234",
        )
        assert "diag-secret-value-42" not in text
        assert "profile-a" in text
        assert "0.0.0-test" in text
        assert "abc1234" in text
        assert "ok" in text
    finally:
        diagnostics.clear_secrets()


def test_build_diagnostics_handles_no_profile():
    text = diagnostics.build_diagnostics(
        profile=None, catalog_status="stale", version="0.0.0", commit="deadbee"
    )
    assert "none" in text.lower()
    assert "stale" in text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_log_dir_and_files_are_private_including_after_rotation(monkeypatch):
    log_dir = paths.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(log_dir, 0o755)
    stale = log_dir / "app.log.1"
    stale.write_text("old\n", encoding="utf-8")
    os.chmod(stale, 0o644)
    monkeypatch.setattr(diagnostics, "_configured_log_file", None)
    logger = diagnostics.setup_logging()
    logger.info("hello")
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((log_dir / "app.log").stat().st_mode) == 0o600
    assert stat.S_IMODE(stale.stat().st_mode) == 0o600
    (handler,) = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    handler.doRollover()
    logger.info("after rollover")
    handler.flush()
    for f in log_dir.glob("app.log*"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
    assert "after rollover" in (log_dir / "app.log").read_text(encoding="utf-8")
