"""Main window: wires pages to QThreadPool workers. Holds the project state
and the session log (FR10). UI code never calls adapters directly on the UI
thread — all adapter/pipeline work goes through Worker."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from typing import Any, Callable

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import pipeline
from ..config import IstariConfig
from ..file_export import (
    build_comparison_csv,
    build_html_report,
    build_tidy_answers_json,
    build_compliance_report_html,
    build_compliance_report_json,
    upload_exports,
)
from ..istari_adapter import (
    CredentialInfo,
    CredentialSelection,
)
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
from .theme import STYLESHEET
from .workers import Worker


def _default_adapter_factory(config: IstariConfig):
    from ..istari_adapter import IstariAdapter

    return IstariAdapter(config)


class MainWindow(QMainWindow):
    def __init__(
        self,
        istari=None,
        *,
        adapter_factory: Callable[[IstariConfig], Any] | None = None,
        registry_url_prefill: str = "",
        pat_prefill: str = "",
        llm_provider: str | None = None,
        llm_model: str | None = None,
        project_dir: Path | None = None,
        poll_interval_s: float = 3.0,
        job_timeout_s: float = 900.0,
        request_timeout_s: float = 60.0,
        retries: int = 2,
        do_custom_extraction: bool = False,
        response_concurrency: int = 1,
    ) -> None:
        """``istari`` may be a ready adapter (tests/fakes) or None — the
        normal flow is: user types Registry URL + PAT into the connection bar
        and clicks Connect, which builds the adapter via ``adapter_factory``
        (default: the real IstariAdapter). The PAT lives in the widget/adapter
        memory only — never on disk."""
        super().__init__()
        self.setWindowTitle("RFI Manager")
        self.resize(1200, 750)
        self.setStyleSheet(STYLESHEET)

        self._istari = istari
        self._adapter_factory = adapter_factory or _default_adapter_factory
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._project_dir = project_dir
        self._poll_interval_s = poll_interval_s
        self._job_timeout_s = job_timeout_s
        self._request_timeout_s = request_timeout_s
        self._retries = retries
        self._do_custom_extraction = do_custom_extraction
        self._response_concurrency = response_concurrency
        self._pool = QThreadPool.globalInstance()
        self._workers: list[Worker] = []  # keep refs while running

        self.project: Project | None = None
        self.project_path: Path | None = None
        self.requirements_artifact: RequirementsArtifact | None = None
        self._stage1_result: Stage1Result | None = None
        self._batch_running = False
        # system-based selection state (docs/SYSTEM_SELECTION.md): the last
        # loaded system/branch, stamped into the project at commit time
        self._system_uuid: str | None = None
        self._system_branch: str | None = None
        self._system_files: list = []  # last listing, for re-greying the RFI
        # monotonically increasing token: only the LATEST file-listing worker
        # may populate the pickers (rapid branch switches must not let a slow
        # older listing overwrite a newer one)
        self._file_load_token = 0

        self.stage1_page = Stage1Page()
        self.review_screen = ReviewScreen()
        self.stage2_page = Stage2Page()
        self.comparison_page = ComparisonPage()
        self._stack = QStackedWidget()
        for page in (
            self.stage1_page,
            self.review_screen,
            self.stage2_page,
            self.comparison_page,
        ):
            self._stack.addWidget(page)

        # File menu: project open actions (moved off the old toolbar for a
        # more standard desktop-app layout — same handlers as before).
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction("Open Project…", self.open_project_dialog)
        file_menu.addAction("Open from RFI UUID…", self.open_from_rfi_dialog)

        # Step navigation: a styled, checkable button row driving _stack.
        # review_screen has no button of its own — it's reached via the
        # Stage 1 extraction flow, not navigated to directly.
        nav_bar = QWidget()
        nav_layout = QHBoxLayout(nav_bar)
        nav_layout.setContentsMargins(12, 4, 12, 0)
        nav_layout.setSpacing(4)
        self._nav_buttons: dict[int, QPushButton] = {}
        for index, page, label in (
            (0, self.stage1_page, "1 · RFI Requirements"),
            (2, self.stage2_page, "2 · Vendor Responses"),
            (3, self.comparison_page, "3 · Comparison"),
        ):
            button = QPushButton(label)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked, p=page: self._stack.setCurrentWidget(p)
            )
            nav_layout.addWidget(button)
            self._nav_buttons[index] = button
        nav_layout.addStretch(1)
        self._nav_buttons[0].setChecked(True)
        self._stack.currentChanged.connect(self._on_stack_page_changed)

        # Connection to the registry comes from the UI, not config.toml
        # (PRD §3.3): URL + PAT typed here, adapter built on Connect. The PAT
        # box is masked and its value never leaves process memory.
        conn_card = QWidget()
        conn_card.setObjectName("card")
        conn_layout = QVBoxLayout(conn_card)
        conn_layout.setContentsMargins(14, 10, 14, 12)
        conn_layout.setSpacing(6)
        title = QLabel("Istari Connection")
        title.setProperty("role", "section")
        conn_layout.addWidget(title)

        conn_row = QHBoxLayout()
        conn_row.setSpacing(8)
        conn_row.addWidget(QLabel("Registry URL"))
        self.registry_url_edit = QLineEdit(registry_url_prefill)
        self.registry_url_edit.setMinimumWidth(260)
        self.registry_url_edit.setPlaceholderText(
            "https://your-instance.istaridigital.com"
        )
        conn_row.addWidget(self.registry_url_edit)
        conn_row.addWidget(QLabel("Personal Access Token"))
        self.pat_edit = QLineEdit(pat_prefill)
        self.pat_edit.setMinimumWidth(200)
        self.pat_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pat_edit.setPlaceholderText("Istari personal access token")
        conn_row.addWidget(self.pat_edit)
        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("primaryButton")
        self.connect_button.clicked.connect(self.connect_to_registry)
        # editing either credential re-arms Connect as the current step
        self.registry_url_edit.textChanged.connect(
            lambda _t: self.connect_button.setEnabled(True)
        )
        self.pat_edit.textChanged.connect(
            lambda _t: self.connect_button.setEnabled(True)
        )
        conn_row.addWidget(self.connect_button)
        self.connection_label = QLabel(
            " connected (injected)" if istari else " not connected"
        )
        self.connection_label.setProperty("role", "hint")
        conn_row.addWidget(self.connection_label)
        conn_row.addStretch(1)
        if istari is not None:  # injected adapter: already connected
            self.connect_button.setEnabled(False)
            self.stage1_page.set_connected(True)
        conn_layout.addLayout(conn_row)

        # Linked Account bound to every LLM job (docs/LLM_Call_Flow.md):
        # populated from list_credentials(), stored by credential id.
        cred_row = QHBoxLayout()
        cred_row.setSpacing(8)
        cred_row.addWidget(QLabel("LLM Credential"))
        self.llm_cred_combo = QComboBox()
        self.llm_cred_combo.setMinimumWidth(220)
        cred_row.addWidget(self.llm_cred_combo)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh_credentials)
        cred_row.addWidget(refresh_button)
        cred_row.addStretch(1)
        conn_layout.addLayout(cred_row)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(12, 8, 12, 12)
        central_layout.setSpacing(10)
        central_layout.addWidget(nav_bar)
        central_layout.addWidget(conn_card)
        central_layout.addWidget(self._stack, 1)
        self.setCentralWidget(central)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Menlo, Consolas, monospace"))
        dock = QDockWidget("Session Log")
        dock.setWidget(self.log_view)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

        self.stage1_page.load_system_requested.connect(self.load_system)
        self.stage1_page.branch_selected.connect(self.load_system_files)
        self.stage1_page.extract_requested.connect(self.start_extraction)
        self.review_screen.commit_requested.connect(self.start_commit)
        self.stage2_page.ingest_requested.connect(self.start_ingest)
        self.stage2_page.retry_requested.connect(self.retry_response)
        self.comparison_page.commit_observations_requested.connect(
            self._on_commit_observations_requested
        )

        if self._istari is not None:  # injected adapter (tests/fakes)
            self.refresh_credentials()

    # ------------------------------------------------------- navigation

    def _on_stack_page_changed(self, index: int) -> None:
        """Keep the step buttons highlighted in sync with _stack, however it
        got switched (a nav click, or code navigating after a job finishes)."""
        for page_index, button in self._nav_buttons.items():
            button.setChecked(page_index == index)

    # --------------------------------------------------------- connection

    def _require_connection(self):
        """The connected adapter, or None with a warning shown."""
        if self._istari is None:
            QMessageBox.warning(
                self,
                "Not connected",
                "Enter the Registry URL and your PAT in the connection bar "
                "and click Connect first.",
            )
            return None
        return self._istari

    def connect_to_registry(self) -> None:
        """Build an adapter from the typed URL + PAT and validate it off the
        UI thread (check_connection -> get_current_user)."""
        url = self.registry_url_edit.text().strip()
        pat = self.pat_edit.text().strip()
        if not url or not pat:
            QMessageBox.warning(
                self, "Missing fields", "Enter both the Registry URL and a PAT."
            )
            return
        config = IstariConfig(
            base_url=url,
            token=pat,
            request_timeout_s=self._request_timeout_s,
            retries=self._retries,
            job_poll_interval_s=self._poll_interval_s,
            job_timeout_s=self._job_timeout_s,
        )
        self.connect_button.setEnabled(False)
        self.connection_label.setText(" connecting…")
        self.log(f"connecting to {url}")
        factory = self._adapter_factory

        def build_and_check():
            adapter = factory(config)
            return adapter, adapter.check_connection()

        worker = Worker(build_and_check)
        worker.signals.finished.connect(self._on_connected)
        worker.signals.failed.connect(self._on_connect_failed)
        self._spawn(worker)

    def _on_connected(self, result) -> None:
        adapter, user = result
        self._istari = adapter
        # progressive-primary flow: Connect stays dimmed once connected (a
        # changed URL/PAT re-arms it); Load System becomes the current step
        self.connect_button.setEnabled(False)
        self.stage1_page.set_connected(True)
        self.connection_label.setText(f" connected as {user}")
        self.log(f"connected to registry as {user}")
        self.refresh_credentials()

    def _on_connect_failed(self, reason: str) -> None:
        self.connect_button.setEnabled(True)
        self.connection_label.setText(" connection failed")
        self.log(f"connection FAILED: {reason}")
        QMessageBox.critical(self, "Connection failed", reason)

    # -------------------------------------------------------- credentials

    def refresh_credentials(self) -> None:
        """Populate the credential pickers from the platform's Linked
        Accounts (list_credentials) off the UI thread."""
        istari = self._require_connection()
        if istari is None:
            return
        worker = Worker(istari.list_credentials)
        worker.signals.finished.connect(self._on_credentials_listed)
        worker.signals.failed.connect(
            lambda reason: self.log(f"credential listing FAILED: {reason}")
        )
        self._spawn(worker)

    def _on_credentials_listed(self, credentials: list[CredentialInfo]) -> None:
        combo = self.llm_cred_combo
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
                if cred.auth_type and "llm" in cred.auth_type.lower():
                    combo.setCurrentIndex(i)
                    break
        self.log(f"{len(credentials)} linked account(s) available")

    def _llm_job_config(self) -> LLMJobConfig | None:
        """Selected credential + configured provider/model defaults; None
        (with a warning) when no credential is selected."""
        llm_id = self.llm_cred_combo.currentData()
        if not llm_id:
            QMessageBox.warning(
                self,
                "No credentials",
                "Select an LLM token in the credentials bar first "
                "(Linked Accounts on the platform).",
            )
            return None
        return LLMJobConfig(
            credentials=CredentialSelection(llm_credential_id=llm_id),
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

    # ---------------------------------------------- system-based selection

    def load_system(self, system_id: str) -> None:
        """List an RFI system's branches for the Stage 1 branch dropdown
        (docs/SYSTEM_SELECTION.md)."""
        istari = self._require_connection()
        if istari is None:
            return
        self.log(f"loading system {system_id}")
        worker = Worker(istari.list_system_branches, system_id)
        worker.signals.finished.connect(
            lambda branches, s=system_id: self._on_branches_listed(s, branches)
        )
        worker.signals.failed.connect(
            lambda reason: QMessageBox.critical(self, "Load system failed", reason)
        )
        self._spawn(worker)

    def _on_branches_listed(self, system_id: str, branches: list) -> None:
        self._system_uuid = system_id
        self.log(f"system {system_id}: {len(branches)} branch(es)")
        self.stage1_page.set_branches(branches)

    def load_system_files(self, system_id: str, branch_name: str) -> None:
        """List a branch's tracked files for both pickers: the Stage 1 RFI
        dropdown and the Stage 2 response checklist. Both pickers show a
        loading spinner (and are cleared) until THIS listing lands — stale
        entries must not be selectable, and a superseded listing is dropped."""
        istari = self._require_connection()
        if istari is None:
            return
        self._file_load_token += 1
        token = self._file_load_token
        self.stage1_page.set_files_loading()
        self.stage2_page.set_files_loading()
        worker = Worker(istari.list_system_files, system_id, branch_name)
        worker.signals.finished.connect(
            lambda files, b=branch_name, t=token: self._on_system_files_listed(
                b, files, t
            )
        )
        worker.signals.failed.connect(
            lambda reason, t=token: self._on_system_files_failed(reason, t)
        )
        self._spawn(worker)

    def _on_system_files_failed(self, reason: str, token: int) -> None:
        if token != self._file_load_token:
            return  # a newer listing is already in flight; keep its spinner
        # clear the loading state — empty pickers, spinners hidden
        self.stage1_page.set_files([])
        self.stage2_page.set_files([], self.project.rfi_uuid if self.project else None)
        QMessageBox.critical(self, "Load files failed", reason)

    def _on_system_files_listed(
        self, branch_name: str, files: list, token: int
    ) -> None:
        if token != self._file_load_token:
            self.log(f"branch {branch_name!r}: listing superseded; dropped")
            return
        self._system_branch = branch_name
        self._system_files = files
        self.log(f"branch {branch_name!r}: {len(files)} file(s)")
        self.stage1_page.set_files(files)
        # the RFI entry is greyed out in the response picker so it cannot be
        # ingested as a response (FR4); before a project exists there is no
        # RFI to grey yet
        rfi_resource_id = self.project.rfi_uuid if self.project else None
        self.stage2_page.set_files(files, rfi_resource_id)

    def start_extraction(self, rfi_uuid: str, revision_id: str) -> None:
        if self._require_connection() is None:
            return
        if self._batch_running:
            QMessageBox.warning(
                self, "Busy", "A batch is running — wait for it to finish."
            )
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
        # (registry URL/PAT are fixed at Connect time — the adapter was built
        # from the connection bar's values; see connect_to_registry)
        self.stage1_page.set_busy(True)
        self.log(f"stage 1: extracting requirements from RFI {rfi_uuid}")
        worker = Worker(
            pipeline.run_stage1_extraction,
            self._istari,
            llm_config,
            rfi_uuid,
            revision_id=revision_id or None,
            do_custom_extraction=self._do_custom_extraction,
            poll_interval_s=self._poll_interval_s,
            job_timeout_s=self._job_timeout_s,
            send_progress=True,
            send_log=True,
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

    def start_commit(
        self, requirements: list[Requirement], schema_version: str
    ) -> None:
        result = self._stage1_result
        if result is None:
            return
        self.review_screen.set_busy(True)
        self.log(
            f"committing {len(requirements)} requirements, schema v{schema_version}"
        )
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
        if self._system_uuid:
            self.project.system_uuid = self._system_uuid
            self.project.system_branch = self._system_branch
        self.project.requirements_artifact_uuid = info.artifact_id
        self.project.requirements_artifact_revision = info.revision_id
        self.project.schema_version = artifact.schema_version
        self.requirements_artifact = artifact
        self._save_project()
        # the RFI is only now known — re-grey its entry in the response
        # picker so it cannot be ingested as a response (FR4)
        if self._system_files:
            self.stage2_page.set_files(self._system_files, self.project.rfi_uuid)
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

    def start_ingest(self, pairs: list[tuple[str, str]], force: bool) -> None:
        """Stage 2 receives pre-resolved (revision_id, resource_id) pairs
        straight from the system listing (docs/SYSTEM_SELECTION.md) — the
        branch-pinned revision, no per-response resolution round-trips."""
        if self._require_connection() is None:
            return
        if self.project is None or self.requirements_artifact is None:
            QMessageBox.warning(
                self, "No schema", "Commit a requirements schema first."
            )
            return
        # belt-and-suspenders: the RFI entry is unselectable in the picker,
        # but never ingest the RFI as a response regardless (FR4)
        pairs = [(rev, uuid) for rev, uuid in pairs if uuid != self.project.rfi_uuid]
        self._start_response_batch(pairs, force)

    def _start_response_batch(
        self, resolved: list[tuple[str, str]], force: bool
    ) -> None:
        if self.project is None:  # project could have been closed meanwhile
            return
        current_schema = self.project.schema_version
        records: list[ResponseRecord] = []
        for revision_id, uuid in resolved:
            record = self.project.response_for(uuid)
            if record is None:
                record = ResponseRecord(uuid=uuid, revision=revision_id)
                self.project.responses.append(record)
            elif (
                force
                or record.revision != revision_id
                or (
                    record.state is PipelineState.DONE
                    and record.schema_version != current_schema
                )
            ):
                # restart from scratch: force re-extract (FR5), a newly
                # requested revision of the same response, or a DONE record
                # answered under an older schema (FR3 re-run) — replace the
                # record, since done has no outgoing edges
                fresh = ResponseRecord(uuid=uuid, revision=revision_id)
                self.project.responses[self.project.responses.index(record)] = fresh
                record = fresh
            elif record.state is PipelineState.FAILED:
                record.transition(PipelineState.QUEUED)
            elif record.state is PipelineState.DONE:
                # same revision+schema: FR5 idempotency, nothing to do
                self.stage2_page.update_status(
                    uuid,
                    record.state.value,
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
        """Process a batch in ONE Qt worker; inside it, up to
        response_concurrency responses run in flight at once
        (pipeline.process_responses — rolling window across the platform's
        agents; project-file writes serialize in persistence). Re-entrancy
        guarded: a second batch (retry click, resume, rebuild) must wait for
        the running one."""
        if self._batch_running:
            QMessageBox.warning(
                self, "Busy", "A batch is already running — " "wait for it to finish."
            )
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
            return pipeline.process_responses(
                istari,
                llm_config,
                project,
                path,
                records,
                req_artifact,
                force=force,
                do_custom_extraction=self._do_custom_extraction,
                concurrency=self._response_concurrency,
                poll_interval_s=poll,
                job_timeout_s=timeout,
                progress=progress,
                log=log,
            )

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
        if self._istari is None:
            return
        istari, project = self._istari, self.project

        def fetch():
            entries = []
            for record in project.responses:
                if record.state is PipelineState.DONE and record.answers_artifact_uuid:
                    entries.append(
                        (record, pipeline.fetch_answers_artifact(istari, record))
                    )
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
            self.requirements_artifact.requirements,
            entries,
            self.project.schema_version,
        )
        self.comparison_page.load(self.requirements_artifact.requirements, rows)
        self.log(f"comparison table: {len(rows)} responses")
        if rows:
            self._stack.setCurrentWidget(self.comparison_page)

    def _on_commit_observations_requested(self) -> None:
        """Build the three export artifacts (answers.csv, answers_tidy.json,
        review.html) from exactly what's on screen and upload them to the
        RFI model — a real Istari call, so it runs on a Worker like every
        other adapter/pipeline operation."""
        if self.project is None:
            return
        istari = self._require_connection()
        if istari is None:
            return
        requirements, rows = self.comparison_page.current_data()
        if not rows:
            QMessageBox.warning(self, "Nothing to commit", "The comparison table is empty.")
            return
        rfi_uuid = self.project.rfi_uuid

        def commit():
            exports = [
                build_comparison_csv(requirements, rows),
                build_tidy_answers_json(requirements, rows),
                build_html_report(requirements, rows),
                build_compliance_report_json(requirements, rows),
                build_compliance_report_html(requirements, rows),
            ]
            upload_exports(istari, rfi_uuid, exports)
            return exports

        worker = Worker(commit)
        worker.signals.finished.connect(self._on_observations_committed)
        worker.signals.failed.connect(
            lambda reason: QMessageBox.critical(self, "Commit failed", reason)
        )
        self._spawn(worker)

    def _on_observations_committed(self, exports: list) -> None:
        names = ", ".join(item.name.value for item in exports)
        self.log(f"committed observations to RFI {self.project.rfi_uuid}: {names}")

    # ------------------------------------------------- open project (FR11)

    def open_project_dialog(self) -> None:
        chosen, _f = QFileDialog.getOpenFileName(
            self, "Open project", "", "RFI project (*.rfiproj)"
        )
        if chosen:
            self.open_project(Path(chosen))

    def open_project(self, path: Path) -> None:
        if self._require_connection() is None:
            return
        if self._batch_running:
            QMessageBox.warning(
                self, "Busy", "A batch is running — wait for it to finish."
            )
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
        # repopulate the selection pickers from the stored system pointer
        # (docs/SYSTEM_SELECTION.md); pre-system project files have none and
        # just need the system id re-entered
        if self.project.system_uuid:
            self._system_uuid = self.project.system_uuid
            self._system_branch = self.project.system_branch
            self.stage1_page.system_edit.setText(self.project.system_uuid)
            if self.project.system_branch:
                self.load_system_files(
                    self.project.system_uuid, self.project.system_branch
                )
            else:
                self.load_system(self.project.system_uuid)
        for record in self.project.responses:
            self.stage2_page.update_status(
                record.uuid, record.state.value, record.error or ""
            )
        incomplete = [r for r in self.project.responses if r.state in RESUMABLE_STATES]
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
        self._system_uuid = None
        self._system_branch = None
        self._system_files = []
        self.review_screen.load([], "1.0")
        self.comparison_page.load([], [])
        self.stage2_page.status_table.setRowCount(0)
        self.stage2_page.set_files([], None)
        self._stack.setCurrentWidget(self.stage1_page)

    def open_from_rfi(self, rfi_uuid: str) -> None:
        if self._require_connection() is None:
            return
        if self._batch_running:
            QMessageBox.warning(
                self, "Busy", "A batch is running — wait for it to finish."
            )
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
