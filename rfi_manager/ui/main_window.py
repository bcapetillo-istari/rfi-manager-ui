"""Main window: wires pages to QThreadPool workers. Holds the project state
and the session log (FR10). UI code never calls adapters directly on the UI
thread — all adapter/pipeline work goes through Worker."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QDockWidget,
    QStackedWidget,
)
from PySide6.QtCore import Qt

from .. import pipeline
from ..models import Project, Requirement
from ..persistence import save_project
from ..pipeline import Stage1Result
from .review_screen import ReviewScreen
from .stage1_page import Stage1Page
from .workers import Worker


class MainWindow(QMainWindow):
    def __init__(
        self,
        istari,
        llm,
        *,
        project_dir: Path | None = None,
        poll_interval_s: float = 3.0,
        job_timeout_s: float = 900.0,
    ) -> None:
        super().__init__()
        self.setWindowTitle("RFI Manager")
        self.resize(1100, 700)

        self._istari = istari
        self._llm = llm
        self._project_dir = project_dir
        self._poll_interval_s = poll_interval_s
        self._job_timeout_s = job_timeout_s
        self._pool = QThreadPool.globalInstance()
        self._workers: list[Worker] = []  # keep refs while running

        self.project: Project | None = None
        self.project_path: Path | None = None
        self._stage1_result: Stage1Result | None = None

        self.stage1_page = Stage1Page()
        self.review_screen = ReviewScreen()
        self._stack = QStackedWidget()
        self._stack.addWidget(self.stage1_page)
        self._stack.addWidget(self.review_screen)
        self.setCentralWidget(self._stack)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        dock = QDockWidget("Session log")
        dock.setWidget(self.log_view)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

        self.stage1_page.extract_requested.connect(self.start_extraction)
        self.review_screen.commit_requested.connect(self.start_commit)

    # --------------------------------------------------------------- log

    def log(self, message: str) -> None:
        """Session log (FR10): every SDK call outcome, job id, artifact UUID."""
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {message}")

    # ------------------------------------------------------------ helpers

    def _spawn(self, worker: Worker) -> None:
        self._workers.append(worker)
        done = lambda *_a, w=worker: self._workers.remove(w)  # noqa: E731
        worker.signals.finished.connect(done)
        worker.signals.failed.connect(done)
        self._pool.start(worker)

    # ------------------------------------------------------------ stage 1

    def start_extraction(self, rfi_uuid: str, revision_id: str) -> None:
        if self.project is not None and self.project.schema_version:
            answer = QMessageBox.question(
                self,
                "Schema already committed",
                "A requirements schema is already committed for this RFI.\n"
                "Re-running Stage 1 will commit a NEW schema version; existing "
                "response rows keep their original stamp and will be flagged "
                "stale. Continue?",
            )
            if answer is not QMessageBox.StandardButton.Yes:
                self.log("stage 1 re-run cancelled by user")
                return
        self.stage1_page.set_busy(True)
        self.log(f"stage 1: extracting requirements from RFI {rfi_uuid}")
        worker = Worker(
            pipeline.run_stage1_extraction,
            self._istari,
            self._llm,
            rfi_uuid,
            revision_id=revision_id or None,
            poll_interval_s=self._poll_interval_s,
            job_timeout_s=self._job_timeout_s,
            send_progress=True,
        )
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_extraction_done)
        worker.signals.failed.connect(self._on_extraction_failed)
        self._spawn(worker)

    def _on_progress(self, state: str, detail: str) -> None:
        self.stage1_page.show_progress(state, detail)
        self.log(f"{state}: {detail}")

    def _on_extraction_done(self, result: Stage1Result) -> None:
        self._stage1_result = result
        self.stage1_page.set_busy(False)
        for warning in result.warnings:
            self.log(f"warning: {warning}")
        self.log(f"stage 1: {len(result.requirements)} requirements extracted — review")
        schema = (
            pipeline.next_schema_version(self.project.schema_version)
            if self.project is not None and self.project.schema_version
            else "1.0"
        )
        self.review_screen.load(result.requirements, schema)
        self._stack.setCurrentWidget(self.review_screen)

    def _on_extraction_failed(self, reason: str) -> None:
        self.stage1_page.set_busy(False)
        self.stage1_page.show_progress("failed", reason)
        self.log(f"stage 1 FAILED: {reason}")
        QMessageBox.critical(self, "Extraction failed", reason)

    # ------------------------------------------------------------- commit

    def start_commit(self, requirements: list[Requirement], schema_version: str) -> None:
        result = self._stage1_result
        if result is None:
            return
        self.review_screen.set_busy(True)
        self.log(f"committing {len(requirements)} requirements, schema v{schema_version}")
        worker = Worker(
            pipeline.commit_requirements,
            self._istari,
            rfi=result.rfi,
            rfi_revision_id=result.rfi_revision_id,
            requirements=requirements,
            schema_version=schema_version,
            llm_model=result.llm_model,
            send_progress=True,
        )
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_commit_done)
        worker.signals.failed.connect(self._on_commit_failed)
        self._spawn(worker)

    def _on_commit_done(self, commit_result) -> None:
        artifact, info = commit_result
        self.review_screen.set_busy(False)
        result = self._stage1_result
        assert result is not None
        if self.project is None:
            self.project = Project(
                rfi_uuid=result.rfi.model_id, rfi_revision=result.rfi_revision_id
            )
        self.project.rfi_revision = result.rfi_revision_id
        self.project.requirements_artifact_uuid = info.artifact_id
        self.project.schema_version = artifact.schema_version
        self._save_project()
        self.log(
            f"committed requirements artifact {info.artifact_id} "
            f"(revision {info.revision_id}, schema v{artifact.schema_version})"
        )
        self._stack.setCurrentWidget(self.stage1_page)
        self.stage1_page.show_progress("done", "requirements committed")

    def _on_commit_failed(self, reason: str) -> None:
        self.review_screen.set_busy(False)
        self.log(f"commit FAILED: {reason}")
        QMessageBox.critical(self, "Commit failed", reason)

    # -------------------------------------------------------- persistence

    def _save_project(self) -> None:
        """Write the pointer cache atomically at every state transition."""
        assert self.project is not None
        if self.project_path is None:
            default_name = f"{self.project.rfi_uuid}.rfiproj"
            if self._project_dir is not None:
                self.project_path = self._project_dir / default_name
            else:
                chosen, _filter = QFileDialog.getSaveFileName(
                    self, "Save project file", default_name, "RFI project (*.rfiproj)"
                )
                if not chosen:
                    self.log("WARNING: project file not saved (no path chosen)")
                    return
                self.project_path = Path(chosen)
        save_project(self.project, self.project_path)
        self.log(f"project saved: {self.project_path}")
