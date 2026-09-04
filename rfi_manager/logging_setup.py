"""Application logging: one ``rfi_manager`` logger writing to a size-bounded
rotating file with a redaction filter, falling back to stderr if the file
can't be opened. Location is LOG_FILE_LOCATION, else the per-user platform log
dir. The UI session-log panel is fed separately (main_window.log).
"""

from __future__ import annotations

import logging
import sys
import threading
import traceback
import os
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


def _format_exc(exc_type, exc_value, exc_tb) -> str:
    """Redacted traceback string. The one place we deliberately keep a full
    traceback: an uncaught crash is undebuggable without its location, and it
    goes only to the local (redacted) log file, never a user-facing surface."""
    return redact("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))


def _show_crash_dialog(exc_value: BaseException, log_path: Path | None) -> None:
    """A friendly, message-only dialog (never the traceback) so the app names
    the failure instead of vanishing — only if a QApplication exists."""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            return
        where = f"\n\nDetails were written to the log:\n{log_path}" if log_path else ""
        QMessageBox.critical(
            None,
            "Unexpected error",
            "An unexpected error occurred and has been logged.\n\n"
            f"{type(exc_value).__name__}: {redact(str(exc_value))}{where}",
        )
    except Exception:
        pass  # a crash handler must never itself raise


def install_exception_handlers(log_path: Path | None = None) -> None:
    """Route uncaught exceptions to the log file (with a dialog on the UI
    thread) instead of a traceback to a stderr that a packaged app doesn't
    have. Call once, after QApplication exists so the dialog can show."""
    logger = get_logger()

    def main_thread_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("uncaught exception:\n%s", _format_exc(exc_type, exc_value, exc_tb))
        _show_crash_dialog(exc_value, log_path)

    sys.excepthook = main_thread_hook

    def thread_hook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread else "?"
        logger.critical(
            "uncaught exception in thread %s:\n%s",
            name, _format_exc(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = thread_hook

    # Qt's own C++ messages (qWarning/qCritical/qFatal) into the same log
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        def qt_hook(mode, _context, message):
            msg = f"Qt: {redact(message)}"
            if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                logger.error(msg)
            elif mode == QtMsgType.QtWarningMsg:
                logger.warning(msg)
            else:
                logger.debug(msg)

        qInstallMessageHandler(qt_hook)
    except Exception:
        pass  # Qt message routing is best-effort; never block startup
