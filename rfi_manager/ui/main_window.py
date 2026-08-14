"""Main window: wires pages to QThreadPool workers. Holds the project state
and the session log (FR10). UI code never calls adapters directly on the UI
thread — all adapter/pipeline work goes through Worker."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QStackedWidget,
    QToolBar,
)

from .. import pipeline
from ..istari_adapter import CredentialInfo, CredentialSelection
from ..models import (
    RESUMABLE_STATES,
    PipelineState,
    Project,
    Requirement,
    RequirementsArtifact,
    ResponseRecord,
)
from ..persistence import load_project, save_project
from ..pipeline import LLMJobConfig, Stage1Result
from .comparison_page import ComparisonPage
from .review_screen import ReviewScreen
from .stage1_page import Stage1Page
from .stage2_page import Stage2Page
from .workers import Worker


class MainWindow(QMainWindow):
    def __init__(
        self,
        istari,
        *,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        project_dir: Path | None = None,
        poll_interval_s: float = 3.0,
        job_timeout_s: float = 900.0,
    ) -> None:
        super().__init__()
        self.setWindowTitle("RFI Manager")
        self.resize(1200, 750)

        self._istari = istari
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._project_dir = project_dir
        self._poll_interval_s = poll_interval_s
        self._job_timeout_s = job_timeout_s
        self._pool = QThreadPool.globalInstance()
        self._workers: list[Worker] = []  # keep refs while running

        self.project: Project | None = None
        self.project_path: Path | None = None
        self.requirements_artifact: RequirementsArtifact | None = None
        self._stage1_result: Stage1Result | None = None
        self._batch_running = False

        self.stage1_page = Stage1Page()
        self.review_screen = ReviewScreen()
        self.stage2_page = Stage2Page()
        self.comparison_page = ComparisonPage()
        self._stack = QStackedWidget()
        for page in (self.stage1_page, self.review_screen, self.stage2_page,
                     self.comparison_page):
            self._stack.addWidget(page)
        self.setCentralWidget(self._stack)

        toolbar = QToolBar("Navigation")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        toolbar.addAction("Stage 1: RFI", lambda: self._stack.setCurrentWidget(self.stage1_page))
        toolbar.addAction("Stage 2: Responses", lambda: self._stack.setCurrentWidget(self.stage2_page))
        toolbar.addAction("Compare", lambda: self._stack.setCurrentWidget(self.comparison_page))
        toolbar.addSeparator()
        toolbar.addAction("Open project…", self.open_project_dialog)
        toolbar.addAction("Open from RFI UUID…", self.open_from_rfi_dialog)

        # Linked Accounts bound to every LLM job (docs/LLM_Call_Flow.md):
        # populated from list_credentials(), stored by credential id.
        cred_bar = QToolBar("Credentials")
        cred_bar.setMovable(False)
        self.addToolBar(cred_bar)
        cred_bar.addWidget(QLabel(" Istari token: "))
        self.istari_cred_combo = QComboBox()
        self.istari_cred_combo.setMinimumWidth(160)
        cred_bar.addWidget(self.istari_cred_combo)
        cred_bar.addWidget(QLabel(" LLM token: "))
        self.llm_cred_combo = QComboBox()
        self.llm_cred_combo.setMinimumWidth(160)
        cred_bar.addWidget(self.llm_cred_combo)
        cred_bar.addAction("Refresh", self.refresh_credentials)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        dock = QDockWidget("Session log")
        dock.setWidget(self.log_view)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

        self.stage1_page.extract_requested.connect(self.start_extraction)
        self.review_screen.commit_requested.connect(self.start_commit)
        self.stage2_page.ingest_requested.connect(self.start_ingest)
        self.stage2_page.retry_requested.connect(self.retry_response)

        self.refresh_credentials()

    # -------------------------------------------------------- credentials

    def refresh_credentials(self) -> None:
        """Populate the credential pickers from the platform's Linked
        Accounts (list_credentials) off the UI thread."""
        istari = self._istari
        worker = Worker(istari.list_credentials)
        worker.signals.finished.connect(self._on_credentials_listed)
        worker.signals.failed.connect(
            lambda reason: self.log(f"credential listing FAILED: {reason}")
        )
        self._spawn(worker)

    def _on_credentials_listed(self, credentials: list[CredentialInfo]) -> None:
        for combo, wanted in ((self.istari_cred_combo, "istari"),
                              (self.llm_cred_combo, "llm")):
            selected = combo.currentData()
            combo.clear()
            for cred in credentials:
                label = cred.name + (f" ({cred.auth_type})" if cred.auth_type else "")
                combo.addItem(label, cred.credential_id)
            restored = False
            if selected is not None:
                index = combo.findData(selected)
                if index >= 0:  # keep the user's manual choice across refreshes
                    combo.setCurrentIndex(index)
                    restored = True
            if not restored:  # heuristic preselect by auth_type tag
                for i, cred in enumerate(credentials):
                    if cred.auth_type and wanted in cred.auth_type.lower():
                        combo.setCurrentIndex(i)
                        break
        self.log(f"{len(credentials)} linked account(s) available")

    def _llm_job_config(self) -> LLMJobConfig | None:
        """Selected credentials + configured provider/model defaults; None
        (with a warning) when no credentials are selected."""
        istari_id = self.istari_cred_combo.currentData()
        llm_id = self.llm_cred_combo.currentData()
        if not istari_id or not llm_id:
            QMessageBox.warning(
                self, "No credentials",
                "Select an Istari token and an LLM token in the credentials "
                "bar first (Linked Accounts on the platform).",
            )
            return None
        return LLMJobConfig(
            credentials=CredentialSelection(
                istari_credential_id=istari_id, llm_credential_id=llm_id
            ),
            provider=self._llm_provider,
            model=self._llm_model,
        )

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
        worker.signals.log.connect(self.log)
        self._pool.start(worker)

    # ------------------------------------------------------------ stage 1

    def start_extraction(self, rfi_uuid: str, revision_id: str) -> None:
        if self._batch_running:
            QMessageBox.warning(self, "Busy", "A batch is running — wait for it to finish.")
            return
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
        llm_config = self._llm_job_config()
        if llm_config is None:
            return
        self.stage1_page.set_busy(True)
        self.log(f"stage 1: extracting requirements from RFI {rfi_uuid}")
        worker = Worker(
            pipeline.run_stage1_extraction,
            self._istari,
            llm_config,
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
        # bind THIS extraction's result to the handler — re-running Stage 1
        # while a commit is in flight must not cross-stamp results
        worker.signals.finished.connect(
            lambda commit_result, r=result: self._on_commit_done(commit_result, r)
        )
        worker.signals.failed.connect(self._on_commit_failed)
        self._spawn(worker)

    def _on_commit_done(self, commit_result, result: Stage1Result) -> None:
        artifact, info = commit_result
        self.review_screen.set_busy(False)
        if self.project is not None and self.project.rfi_uuid != result.rfi.model_id:
            # the user switched projects while the commit was in flight —
            # the artifact is on the platform, but don't stamp it into an
            # unrelated project file
            self.log(
                f"committed requirements artifact {info.artifact_id} for RFI "
                f"{result.rfi.model_id}, but the open project is "
                f"{self.project.rfi_uuid} — not recorded locally"
            )
            return
        if self.project is None:
            self.project = Project(
                rfi_uuid=result.rfi.model_id, rfi_revision=result.rfi_revision_id
            )
        self.project.rfi_revision = result.rfi_revision_id
        self.project.requirements_artifact_uuid = info.artifact_id
        self.project.requirements_artifact_revision = info.revision_id
        self.project.schema_version = artifact.schema_version
        self.requirements_artifact = artifact
        self._save_project()
        self.log(
            f"committed requirements artifact {info.artifact_id} "
            f"(revision {info.revision_id}, schema v{artifact.schema_version})"
        )
        self._stack.setCurrentWidget(self.stage2_page)
        self.stage1_page.show_progress("done", "requirements committed")

    def _on_commit_failed(self, reason: str) -> None:
        self.review_screen.set_busy(False)
        self.log(f"commit FAILED: {reason}")
        QMessageBox.critical(self, "Commit failed", reason)

    # ------------------------------------------------------------ stage 2

    def start_ingest(self, uuids: list[str], force: bool) -> None:
        if self.project is None or self.requirements_artifact is None:
            QMessageBox.warning(self, "No schema", "Commit a requirements schema first.")
            return
        current_schema = self.project.schema_version
        records: list[ResponseRecord] = []
        for uuid in uuids:
            record = self.project.response_for(uuid)
            if record is None:
                record = ResponseRecord(uuid=uuid)
                self.project.responses.append(record)
            elif force or (
                record.state is PipelineState.DONE
                and record.schema_version != current_schema
            ):
                # restart from scratch: force re-extract (FR5), or a DONE
                # record answered under an older schema (FR3 re-run) —
                # replace the record, since done has no outgoing edges
                fresh = ResponseRecord(uuid=uuid)
                self.project.responses[self.project.responses.index(record)] = fresh
                record = fresh
            elif record.state is PipelineState.FAILED:
                record.transition(PipelineState.QUEUED)
            elif record.state is PipelineState.DONE:
                # same revision+schema: FR5 idempotency, nothing to do
                self.stage2_page.update_status(
                    uuid, record.state.value,
                    "already done for this schema — use Force re-extract",
                )
                continue
            records.append(record)
            self.stage2_page.update_status(uuid, record.state.value, "")
        if records:
            self._run_responses(records, force=force)

    def retry_response(self, uuid: str) -> None:
        """FR4: retry a failed response."""
        if self._batch_running:  # belt-and-suspenders; the button is disabled too
            return
        if self.project is None or self.requirements_artifact is None:
            return
        record = self.project.response_for(uuid)
        if record is None or record.state is not PipelineState.FAILED:
            return
        path = self._ensure_project_path()
        if path is None:
            return
        pipeline.retry_response(record, self.project, path)
        self.log(f"retrying response {uuid}")
        self._run_responses([record], force=True)

    def _run_responses(self, records: list[ResponseRecord], *, force: bool) -> None:
        """Process a batch sequentially in ONE worker so project-file writes
        stay single-threaded. Re-entrancy guarded: a second batch (retry click,
        resume, rebuild) must wait for the running one."""
        if self._batch_running:
            QMessageBox.warning(self, "Busy", "A batch is already running — "
                                "wait for it to finish.")
            return
        llm_config = self._llm_job_config()
        if llm_config is None:
            return
        project, path = self.project, self._ensure_project_path()
        if path is None:
            return
        istari = self._istari
        req_artifact = self.requirements_artifact
        poll, timeout = self._poll_interval_s, self._job_timeout_s

        def batch(progress=None, log=None):
            for record in records:
                per_response = (
                    (lambda s, d, u=record.uuid: progress(s, f"{u}::{d}"))
                    if progress else None
                )
                pipeline.process_response(
                    istari, llm_config, project, path, record, req_artifact,
                    force=force, poll_interval_s=poll, job_timeout_s=timeout,
                    progress=per_response, log=log,
                )
            return records

        self._batch_running = True
        self.stage2_page.set_busy(True)
        worker = Worker(batch, send_progress=True, send_log=True)
        worker.signals.progress.connect(self._on_response_progress)
        worker.signals.finished.connect(self._on_batch_done)
        worker.signals.failed.connect(self._on_batch_failed)
        self._spawn(worker)

    def _on_response_progress(self, state: str, detail: str) -> None:
        uuid, _, message = detail.partition("::")
        self.stage2_page.update_status(uuid, state, message)
        self.log(f"{state}: {uuid} {message}".rstrip())

    def _on_batch_done(self, records: list[ResponseRecord]) -> None:
        self._batch_running = False
        self.stage2_page.set_busy(False)
        for record in records:
            self.stage2_page.update_status(
                record.uuid, record.state.value, record.error or ""
            )
        done = sum(1 for r in records if r.state is PipelineState.DONE)
        self.log(f"batch finished: {done}/{len(records)} done")
        self.refresh_comparison()

    def _on_batch_failed(self, reason: str) -> None:
        self._batch_running = False
        self.stage2_page.set_busy(False)
        self.log(f"batch FAILED unexpectedly: {reason}")
        QMessageBox.critical(self, "Ingest failed", reason)

    # --------------------------------------------------------- comparison

    def refresh_comparison(self) -> None:
        """Re-fetch answers artifacts from Istari and rebuild the table
        (content is never read from the project file — PRD §3.6a)."""
        if self.project is None or self.requirements_artifact is None:
            return
        istari, project = self._istari, self.project

        def fetch():
            entries = []
            for record in project.responses:
                if record.state is PipelineState.DONE and record.answers_artifact_uuid:
                    entries.append((record, pipeline.fetch_answers_artifact(istari, record)))
            return entries

        worker = Worker(fetch)
        worker.signals.finished.connect(self._on_comparison_fetched)
        worker.signals.failed.connect(
            lambda reason: self.log(f"comparison refresh FAILED: {reason}")
        )
        self._spawn(worker)

    def _on_comparison_fetched(self, entries) -> None:
        assert self.project is not None and self.requirements_artifact is not None
        rows = pipeline.build_comparison_rows(
            self.requirements_artifact.requirements, entries, self.project.schema_version
        )
        self.comparison_page.load(self.requirements_artifact.requirements, rows)
        self.log(f"comparison table: {len(rows)} responses")
        if rows:
            self._stack.setCurrentWidget(self.comparison_page)

    # ------------------------------------------------- open project (FR11)

    def open_project_dialog(self) -> None:
        chosen, _f = QFileDialog.getOpenFileName(
            self, "Open project", "", "RFI project (*.rfiproj)"
        )
        if chosen:
            self.open_project(Path(chosen))

    def open_project(self, path: Path) -> None:
        if self._batch_running:
            QMessageBox.warning(self, "Busy", "A batch is running — wait for it to finish.")
            return
        try:
            project = load_project(path)
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "Open failed", str(e))
            return
        self._reset_session_state()
        self.project = project
        self.project_path = path
        self.log(f"opened project {path} (RFI {project.rfi_uuid})")

        istari = self._istari

        def reload():
            return pipeline.fetch_requirements_artifact(istari, project)

        worker = Worker(reload)
        worker.signals.finished.connect(self._on_project_reloaded)
        worker.signals.failed.connect(
            lambda reason: QMessageBox.critical(self, "Reload failed", reason)
        )
        self._spawn(worker)

    def _on_project_reloaded(self, artifact: RequirementsArtifact) -> None:
        assert self.project is not None
        self.requirements_artifact = artifact
        self.log(
            f"reloaded requirements artifact "
            f"{self.project.requirements_artifact_uuid} (schema v{artifact.schema_version})"
        )
        for record in self.project.responses:
            self.stage2_page.update_status(
                record.uuid, record.state.value, record.error or ""
            )
        incomplete = [
            r for r in self.project.responses if r.state in RESUMABLE_STATES
        ]
        if incomplete:
            answer = QMessageBox.question(
                self,
                "Resume",
                f"Resume {len(incomplete)} incomplete extraction"
                f"{'s' if len(incomplete) != 1 else ''}?",
            )
            if answer is QMessageBox.StandardButton.Yes:
                self.log(f"resuming {len(incomplete)} incomplete responses (FR11)")
                self._stack.setCurrentWidget(self.stage2_page)
                self._run_responses(incomplete, force=False)
                return
        self.refresh_comparison()

    # --------------------------------------------- open from RFI UUID (FR12)

    def open_from_rfi_dialog(self) -> None:
        rfi_uuid, ok = QInputDialog.getText(
            self, "Open from RFI UUID", "Istari UUID of the RFI file:"
        )
        if ok and rfi_uuid.strip():
            self.open_from_rfi(rfi_uuid.strip())

    def _reset_session_state(self) -> None:
        """Clear all state tied to the previous project so nothing stale can
        leak across a project switch (review screen, uncommitted stage-1
        result, schema of record, comparison table, status list)."""
        self._stage1_result = None
        self.requirements_artifact = None
        self.review_screen.load([], "1.0")
        self.comparison_page.load([], [])
        self.stage2_page.status_table.setRowCount(0)
        self._stack.setCurrentWidget(self.stage1_page)

    def open_from_rfi(self, rfi_uuid: str) -> None:
        if self._batch_running:
            QMessageBox.warning(self, "Busy", "A batch is running — wait for it to finish.")
            return
        self.log(f"rebuilding project from platform for RFI {rfi_uuid} (FR12)")
        istari = self._istari

        def rebuild(log=None):
            return pipeline.rebuild_from_platform(istari, rfi_uuid, log=log)

        worker = Worker(rebuild, send_log=True)
        worker.signals.finished.connect(self._on_rebuilt)
        worker.signals.failed.connect(
            lambda reason: QMessageBox.critical(self, "Rebuild failed", reason)
        )
        self._spawn(worker)

    def _on_rebuilt(self, result) -> None:
        project, artifact = result
        self._reset_session_state()
        self.project = project
        self.project_path = None
        self.requirements_artifact = artifact
        self.log(
            f"rebuilt project: {len(project.responses)} responses, "
            f"schema v{project.schema_version}"
        )
        self._save_project()
        for record in project.responses:
            self.stage2_page.update_status(record.uuid, record.state.value, "")
        self.refresh_comparison()

    # -------------------------------------------------------- persistence

    def _ensure_project_path(self) -> Path | None:
        if self.project is None:
            return None
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
                    return None
                self.project_path = Path(chosen)
        return self.project_path

    def _save_project(self) -> None:
        """Write the pointer cache atomically at every state transition."""
        assert self.project is not None
        path = self._ensure_project_path()
        if path is None:
            return
        save_project(self.project, path)
        self.log(f"project saved: {path}")
