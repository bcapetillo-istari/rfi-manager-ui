"""Stage 2 page (FR4): single-UUID field plus batch box (one UUID per line),
per-response status list, failure reasons with a retry action, and a
"Force re-extract" toggle (FR5)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

_COLUMNS = ["Response UUID", "State", "Detail"]


class Stage2Page(QWidget):
    ingest_requested = Signal(list, bool)  # (revision_ids, force)
    retry_requested = Signal(str)  # response uuid (model id)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Ingest Vendor Responses")
        title.setProperty("role", "section")
        layout.addWidget(title)
        hint = QLabel(
            "Enter one or more response file revision UUIDs to extract "
            "answers against the committed requirements schema."
        )
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addWidget(QLabel("Response Revision UUID"))
        self.single_edit = QLineEdit()
        self.single_edit.setPlaceholderText("Istari Revision UUID of one response PDF")
        layout.addWidget(self.single_edit)

        layout.addWidget(QLabel("Batch (one Revision UUID per line)"))
        self.batch_edit = QPlainTextEdit()
        self.batch_edit.setMaximumHeight(90)
        layout.addWidget(self.batch_edit)

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

    # -------------------------------------------------------------- input

    def _collect_revision_ids(self) -> list[str]:
        revision_ids = []
        if self.single_edit.text().strip():
            revision_ids.append(self.single_edit.text().strip())
        for line in self.batch_edit.toPlainText().splitlines():
            if line.strip():
                revision_ids.append(line.strip())
        seen: set[str] = set()
        return [r for r in revision_ids if not (r in seen or seen.add(r))]

    def _on_ingest(self) -> None:
        revision_ids = self._collect_revision_ids()
        if revision_ids:
            self.ingest_requested.emit(revision_ids, self.force_check.isChecked())

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
        self.ingest_button.setEnabled(not busy)
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
