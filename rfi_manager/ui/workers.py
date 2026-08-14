"""QThreadPool workers. The UI thread never blocks (CLAUDE.md rule): every
adapter/pipeline call runs in a Worker; the UI reacts to its signals."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class WorkerSignals(QObject):
    progress = Signal(str, str)  # (state, detail) per PRD §3.2
    finished = Signal(object)  # the callable's return value
    failed = Signal(str)  # user-actionable reason (FR10)


class Worker(QRunnable):
    """Run ``fn(*args, **kwargs)`` on the thread pool.

    With ``send_progress=True`` the callable is invoked with an extra
    ``progress=self.signals.progress.emit`` kwarg (all pipeline stage
    functions accept it) — signal emission is thread-safe.
    """

    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        send_progress: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.signals = WorkerSignals()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        if send_progress:
            self._kwargs["progress"] = self.signals.progress.emit

    @Slot()
    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as e:  # surfaced in the UI; no silent failures (FR10)
            self.signals.failed.emit(str(e))
            return
        self.signals.finished.emit(result)
