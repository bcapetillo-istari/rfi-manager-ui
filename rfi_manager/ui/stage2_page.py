"""Stage 2 page (FR4, docs/SYSTEM_SELECTION.md): checkable list of the
system branch's files (all checked by default; the RFI's own entry greyed
out so it cannot be ingested as a response), Select All/None, selected-count
label, per-response status list, failure reasons with a retry action, and a
"Force re-extract" toggle (FR5)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..istari_adapter import SystemFileInfo

_COLUMNS = ["Response UUID", "State", "Detail"]


class Stage2Page(QWidget):
    # list of (revision_id, resource_id) pairs — pre-resolved by the system
    # listing, so no per-response revision resolution is needed
    ingest_requested = Signal(list, bool)  # (pairs, force)
    retry_requested = Signal(str)  # response uuid (model id)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._busy = False
        self._rfi_resource_id: str | None = None
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Ingest Vendor Responses")
        title.setProperty("role", "section")
        layout.addWidget(title)
        hint = QLabel(
            "Select which of the system's files are vendor responses, then "
            "extract answers against the committed requirements schema. The "
            "RFI itself cannot be selected."
        )
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # indeterminate spinner shown while a branch's files are loading, so
        # stale entries can't be checked/ingested before the new listing lands
        self.file_loading = QProgressBar()
        self.file_loading.setRange(0, 0)
        self.file_loading.setTextVisible(False)
        self.file_loading.setMaximumHeight(8)
        self.file_loading.hide()
        layout.addWidget(self.file_loading)

        self.file_list = QListWidget()
        self.file_list.itemChanged.connect(lambda _item: self._update_count())
        layout.addWidget(self.file_list)

        select_row = QHBoxLayout()
        select_row.setSpacing(8)
        self.select_all_button = QPushButton("Select All")
        self.select_all_button.clicked.connect(lambda: self._set_all(True))
        select_row.addWidget(self.select_all_button)
        self.select_none_button = QPushButton("Select None")
        self.select_none_button.clicked.connect(lambda: self._set_all(False))
        select_row.addWidget(self.select_none_button)
        self.count_label = QLabel("0 of 0 selected")
        self.count_label.setProperty("role", "hint")
        select_row.addWidget(self.count_label)
        select_row.addStretch(1)
        layout.addLayout(select_row)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.force_check = QCheckBox("Force re-extract")
        controls.addWidget(self.force_check)
        self.ingest_button = QPushButton("Ingest Responses")
        self.ingest_button.setObjectName("primaryButton")
        self.ingest_button.clicked.connect(self._on_ingest)
        controls.addWidget(self.ingest_button)
        self.retry_button = QPushButton("Retry Selected")
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self._on_retry)
        controls.addWidget(self.retry_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.status_table = QTableWidget(0, len(_COLUMNS))
        self.status_table.setHorizontalHeaderLabels(_COLUMNS)
        self.status_table.horizontalHeader().resizeSection(0, 260)
        self.status_table.horizontalHeader().resizeSection(1, 90)
        self.status_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.status_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.status_table.setAlternatingRowColors(True)
        self.status_table.verticalHeader().setVisible(False)
        self.status_table.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self.status_table)

    # ---------------------------------------------------------- file list

    def set_files_loading(self) -> None:
        """Branch files are being fetched: clear + disable the checklist so
        stale entries can't be checked/ingested, and show the spinner."""
        self.file_list.blockSignals(True)
        self.file_list.clear()
        self.file_list.blockSignals(False)
        self.file_loading.show()
        self.count_label.setText("Loading branch files…")
        self.ingest_button.setEnabled(False)

    def set_files(
        self, files: list[SystemFileInfo], rfi_resource_id: str | None
    ) -> None:
        """Populate the response picker from the system listing: everything
        checked by default EXCEPT the RFI's own entry, which is unchecked,
        disabled, and greyed so it cannot be ingested as a response."""
        self._rfi_resource_id = rfi_resource_id
        self.file_loading.hide()
        self.file_list.blockSignals(True)
        self.file_list.clear()
        # only Models are ingestable — same filter as the Stage 1 RFI picker
        # (screens out artifacts and the pipeline's staged-text models)
        files = [f for f in files if f.is_selectable]
        for f in files:
            item = QListWidgetItem(f.name)
            item.setData(Qt.ItemDataRole.UserRole, f)
            if f.resource_id == rfi_resource_id:
                # greyed out, no extra text — the explanation lives in the
                # tooltip instead (explicit foreground: the app stylesheet
                # keeps disabled items near-normal colored otherwise)
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setForeground(QBrush(QColor(160, 160, 160)))
                item.setToolTip("This is the RFI — it cannot be selected as a response.")
            else:
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(Qt.CheckState.Checked)
            self.file_list.addItem(item)
        self.file_list.blockSignals(False)
        self._update_count()

    def _selectable_items(self) -> list[QListWidgetItem]:
        return [
            self.file_list.item(i)
            for i in range(self.file_list.count())
            if self.file_list.item(i).flags() & Qt.ItemFlag.ItemIsUserCheckable
        ]

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.file_list.blockSignals(True)
        for item in self._selectable_items():
            item.setCheckState(state)
        self.file_list.blockSignals(False)
        self._update_count()

    def selected_pairs(self) -> list[tuple[str, str]]:
        """The checked entries as (revision_id, resource_id) pairs."""
        pairs = []
        for item in self._selectable_items():
            if item.checkState() is Qt.CheckState.Checked:
                info: SystemFileInfo = item.data(Qt.ItemDataRole.UserRole)
                pairs.append((info.revision_id, info.resource_id))
        return pairs

    def _update_count(self) -> None:
        selectable = self._selectable_items()
        selected = sum(
            1 for i in selectable if i.checkState() is Qt.CheckState.Checked
        )
        self.count_label.setText(f"{selected} of {len(selectable)} selected")
        self.ingest_button.setEnabled(not self._busy and selected > 0)

    # -------------------------------------------------------------- input

    def _on_ingest(self) -> None:
        pairs = self.selected_pairs()
        if pairs:
            self.ingest_requested.emit(pairs, self.force_check.isChecked())

    def _on_retry(self) -> None:
        row = self.status_table.currentRow()
        if row >= 0:
            uuid_item = self.status_table.item(row, 0)
            if uuid_item:
                self.retry_requested.emit(uuid_item.text())

    def _on_selection(self) -> None:
        row = self.status_table.currentRow()
        state_item = self.status_table.item(row, 1) if row >= 0 else None
        self.retry_button.setEnabled(
            not self._busy and bool(state_item and state_item.text() == "failed")
        )

    # ------------------------------------------------------------- status

    def set_busy(self, busy: bool) -> None:
        """While a batch runs, neither a new ingest nor a retry may start —
        both would spawn a second worker mutating the same project."""
        self._busy = busy
        self._update_count()
        self._on_selection()

    def _row_for(self, uuid: str) -> int:
        for row in range(self.status_table.rowCount()):
            item = self.status_table.item(row, 0)
            if item and item.text() == uuid:
                return row
        row = self.status_table.rowCount()
        self.status_table.insertRow(row)
        self.status_table.setItem(row, 0, QTableWidgetItem(uuid))
        self.status_table.setItem(row, 1, QTableWidgetItem(""))
        self.status_table.setItem(row, 2, QTableWidgetItem(""))
        return row

    def update_status(self, uuid: str, state: str, detail: str) -> None:
        row = self._row_for(uuid)
        self.status_table.item(row, 1).setText(state)
        self.status_table.item(row, 2).setText(detail)
        self._on_selection()
