"""Stage 1 page (FR1, docs/SYSTEM_SELECTION.md): System UUID input ->
branch dropdown -> RFI file dropdown -> "Extract requirements" button with
visible progress states. All platform listing happens in the main window's
Workers; this page only renders results and emits selections."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..istari_adapter import BranchInfo, SystemFileInfo


class Stage1Page(QWidget):
    load_system_requested = Signal(str)  # system uuid
    branch_selected = Signal(str, str)  # (system uuid, branch name)
    extract_requested = Signal(str, str)  # (rfi resource id, revision_id-or-empty)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Extract Requirements from an RFI")
        title.setProperty("role", "section")
        layout.addWidget(title)
        hint = QLabel(
            "Enter the Istari System containing the RFI and its responses, "
            "pick a branch, then select the RFI file and extract its "
            "requirements schema with an LLM job."
        )
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(8)
        system_row = QHBoxLayout()
        system_row.setSpacing(8)
        self.system_edit = QLineEdit()
        self.system_edit.setPlaceholderText("Istari UUID of the RFI System")
        system_row.addWidget(self.system_edit)
        self.load_button = QPushButton("Load System")
        self.load_button.clicked.connect(self._on_load_system)
        system_row.addWidget(self.load_button)
        form.addRow("RFI System UUID", system_row)

        self.branch_combo = QComboBox()
        self.branch_combo.setEnabled(False)
        self.branch_combo.currentTextChanged.connect(self._on_branch_changed)
        form.addRow("Branch", self.branch_combo)

        self.file_combo = QComboBox()
        self.file_combo.setEnabled(False)
        self.file_combo.currentIndexChanged.connect(lambda _i: self._update_buttons())
        form.addRow("RFI File", self.file_combo)

        # indeterminate spinner shown while a branch's files are loading, so
        # stale entries can't be picked before the new listing lands
        self.file_loading = QProgressBar()
        self.file_loading.setRange(0, 0)
        self.file_loading.setTextVisible(False)
        self.file_loading.setMaximumHeight(8)
        self.file_loading.hide()
        form.addRow("", self.file_loading)
        layout.addLayout(form)

        self.extract_button = QPushButton("Extract Requirements")
        self.extract_button.setObjectName("primaryButton")
        self.extract_button.setEnabled(False)
        self.extract_button.clicked.connect(self._on_extract)
        layout.addWidget(self.extract_button)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self._busy = False

    # -------------------------------------------------------------- events

    def _on_load_system(self) -> None:
        system_id = self.system_edit.text().strip()
        if not system_id:
            self.status_label.setText("Enter a System UUID first.")
            return
        self.load_system_requested.emit(system_id)

    def _on_branch_changed(self, branch_name: str) -> None:
        # a programmatic clear() also fires with "" — ignore it
        if branch_name and self.branch_combo.isEnabled():
            self.branch_selected.emit(self.system_edit.text().strip(), branch_name)

    def _on_extract(self) -> None:
        info = self.file_combo.currentData()
        if info is None:
            self.status_label.setText("Select the RFI file first.")
            return
        # the branch-pinned revision rides along so extraction provenance
        # matches what the system snapshot tracks, not "latest"
        self.extract_requested.emit(info.resource_id, info.revision_id)

    # ----------------------------------------------------------- populate

    def set_branches(self, branches: list[BranchInfo]) -> None:
        self.branch_combo.blockSignals(True)
        self.branch_combo.clear()
        for branch in branches:
            self.branch_combo.addItem(branch.name)
        self.branch_combo.blockSignals(False)
        self.branch_combo.setEnabled(bool(branches))
        self.file_combo.clear()
        self.file_combo.setEnabled(False)
        if not branches:
            self.status_label.setText("System has no branches.")
            self._update_buttons()
            return
        # default to "main" when present, else the first branch
        index = max(self.branch_combo.findText("main"), 0)
        self.branch_combo.setCurrentIndex(index)
        # fire the initial listing explicitly — setCurrentIndex(0) after a
        # clear() does not emit currentTextChanged
        self.branch_selected.emit(
            self.system_edit.text().strip(), self.branch_combo.currentText()
        )
        self._update_buttons()

    def set_files_loading(self) -> None:
        """Branch files are being fetched: clear + disable the picker so a
        stale entry can't be selected, and show the spinner."""
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        self.file_combo.blockSignals(False)
        self.file_combo.setEnabled(False)
        self.file_loading.show()
        self.status_label.setText("Loading branch files…")
        self._update_buttons()

    def set_files(self, files: list[SystemFileInfo]) -> None:
        self.file_loading.hide()
        self.file_combo.blockSignals(True)
        self.file_combo.clear()

        files = self._filter_files(files)

        for f in files:
            self.file_combo.addItem(f.name, f)  # item data: SystemFileInfo
        self.file_combo.blockSignals(False)
        self.file_combo.setEnabled(bool(files))
        if not files:
            self.status_label.setText("Branch tracks no files.")
        self._update_buttons()

    def _filter_files(self, files: list[SystemFileInfo]) -> list[SystemFileInfo]:
        """Only Models are selectable — the pipeline can't run on artifacts,
        comments, or other tracked resource types — and the pipeline's own
        staged-text plumbing models are screened out too."""
        return [f for f in files if f.is_selectable]

    def selected_branch(self) -> str:
        return self.branch_combo.currentText()

    # -------------------------------------------------------------- state

    def _update_buttons(self) -> None:
        self.load_button.setEnabled(not self._busy)
        self.extract_button.setEnabled(
            not self._busy
            and self.file_combo.isEnabled()
            and self.file_combo.currentData() is not None
        )

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def show_progress(self, state: str, detail: str) -> None:
        self.status_label.setText(f"[{state}] {detail}")
