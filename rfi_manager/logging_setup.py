"""Application logging: one ``rfi_manager`` logger writing to a size-bounded
rotating file with a redaction filter, falling back to stderr if the file
can't be opened. Location is LOG_FILE_LOCATION, else the per-user platform log
dir. The UI session-log panel is fed separately (main_window.log).
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .redaction import redact

APP_LOGGER = "rfi_manager"
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB per file
_BACKUP_COUNT = 5  # 5 rotated files => ~10 MB cap total
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _RedactionFilter(logging.Filter):
    """Scrub credentials from every record before any handler emits it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None  # already interpolated into msg
        return True


def _platform_log_dir() -> Path:
    """Per-user, always-writable log directory by OS convention."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Logs" / "RFIManager"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "RFIManager" / "logs"
    # Linux / other: XDG state dir
    base = os.environ.get("XDG_STATE_HOME") or str(home / ".local" / "state")
    return Path(base) / "RFIManager"


def resolve_log_dir(override: str | None) -> Path:
    return Path(override).expanduser() if override else _platform_log_dir()


def configure_logging(
    log_dir_override: str | None = None, *, level: int = logging.DEBUG
) -> Path | None:
    """Configure the app logger once. Idempotent (safe to call again; existing
    handlers are cleared first). Returns the resolved log-file path, or None
    if the file handler could not be created (stderr logging still works)."""
    logger = logging.getLogger(APP_LOGGER)
    logger.setLevel(level)
    logger.propagate = False  # don't double-log through the root logger
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    redaction = _RedactionFilter()
    formatter = logging.Formatter(_FORMAT)

    try:
        log_dir = resolve_log_dir(log_dir_override)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "rfi_manager.log"
        file_handler = RotatingFileHandler(
            log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction)
        logger.addHandler(file_handler)
        return log_path
    except OSError:
        # a read-only/locked-down log dir must never stop the app
        fallback = logging.StreamHandler(sys.stderr)
        fallback.setLevel(logging.INFO)
        fallback.setFormatter(formatter)
        fallback.addFilter(redaction)
        logger.addHandler(fallback)
        logger.warning("could not open log file; logging to stderr only")
        return None


def get_logger() -> logging.Logger:
    return logging.getLogger(APP_LOGGER)
