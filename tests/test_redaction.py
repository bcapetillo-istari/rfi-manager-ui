"""redaction.redact / register_secret: scrub credentials, leave ordinary
diagnostic text intact (docs/PRODUCTION_READINESS.md §1)."""

from __future__ import annotations

import logging
import threading

import pytest

from rfi_manager import redaction
from rfi_manager.istari_adapter import IstariError
from rfi_manager.logging_setup import configure_logging
from rfi_manager.pipeline import PipelineError


@pytest.fixture(autouse=True)
def _clean_secrets():
    redaction.clear_secrets()
    yield
    redaction.clear_secrets()


def test_registered_pat_is_scrubbed_even_bare():
    redaction.register_secret("super-secret-pat-value-123")
    out = redaction.redact("connecting with token super-secret-pat-value-123 ok")
    assert "super-secret-pat-value-123" not in out
    assert "***REDACTED***" in out


def test_short_secret_ignored():
    """A trivially short value must not be registered — it would blank out
    unrelated text."""
    redaction.register_secret("abc")
    assert redaction.redact("abc def") == "abc def"


def test_bearer_header_scrubbed():
    out = redaction.redact("401 Unauthorized: Authorization: Bearer eyJhbGci.OiJ.abc-123")
    assert "eyJhbGci" not in out
    assert "***REDACTED***" in out


def test_token_pair_scrubbed():
    assert "sk-9999" not in redaction.redact("request failed api_key=sk-9999xyz")


def test_presigned_url_signature_scrubbed():
    url = "https://bucket.s3.amazonaws.com/f?X-Amz-Signature=deadbeefcafe&X-Amz-Expires=900"
    out = redaction.redact(f"cannot read revision r-1: GET {url}")
    assert "deadbeefcafe" not in out
    assert "X-Amz-Signature=" in out  # the param name stays, value scrubbed
    assert "X-Amz-Expires=900" in out  # non-secret params untouched


@pytest.mark.parametrize("ordinary", [
    "revision r-1 not found on RFI m-2",
    "LLM answers failed validation after retry",
    "model 900fcce4-2f5a-49f4-a489-b9d7a044f112 has no answers_raw.json artifact",
    "job job-7 still Running after 900s",
])
def test_ordinary_text_untouched(ordinary):
    assert redaction.redact(ordinary) == ordinary


def test_exceptions_self_redact():
    """IstariError/PipelineError scrub their own message — this is what keeps
    a token out of record.error (persisted to .rfiproj) and error dialogs."""
    redaction.register_secret("live-pat-abcdef123456")
    assert "live-pat-abcdef123456" not in str(
        IstariError("cannot connect: Bearer live-pat-abcdef123456")
    )
    assert "live-pat-abcdef123456" not in str(
        PipelineError("stage failed: token=live-pat-abcdef123456")
    )


def test_logging_handlers_redact(tmp_path):
    """A record logged with a credential is scrubbed in the log file."""
    redaction.register_secret("logged-pat-value-7777")
    log_path = configure_logging(str(tmp_path))
    assert log_path is not None
    logging.getLogger("rfi_manager").info(
        "connected with token logged-pat-value-7777"
    )
    for h in logging.getLogger("rfi_manager").handlers:
        h.flush()
    written = log_path.read_text(encoding="utf-8")
    assert "logged-pat-value-7777" not in written
    assert "***REDACTED***" in written


# ------------------------------------------- top-level exception handlers


def test_uncaught_traceback_is_redacted_in_log(tmp_path):
    """The crash handler logs a full traceback (crash forensics) but scrubbed
    — a registered secret must not survive into the log file."""
    from rfi_manager import logging_setup

    redaction.register_secret("crash-pat-value-9999")
    configure_logging(str(tmp_path))

    try:
        raise RuntimeError("boom with token crash-pat-value-9999")
    except RuntimeError:
        import sys
        formatted = logging_setup._format_exc(*sys.exc_info())

    assert "crash-pat-value-9999" not in formatted
    assert "***REDACTED***" in formatted
    assert "RuntimeError" in formatted  # the useful crash info is still there


def test_install_exception_handlers_wires_hooks(tmp_path):
    """install_exception_handlers replaces sys/threading excepthooks; restore
    them so this can't corrupt pytest's own error reporting."""
    import sys

    from rfi_manager.logging_setup import install_exception_handlers

    saved_sys, saved_thread = sys.excepthook, threading.excepthook
    try:
        install_exception_handlers(tmp_path / "rfi_manager.log")
        assert sys.excepthook is not saved_sys
        assert threading.excepthook is not saved_thread
    finally:
        sys.excepthook, threading.excepthook = saved_sys, saved_thread
