"""Stage 1 page (FR1): RFI UUID (+ optional revision) input, "Extract
requirements" button, visible progress states. Dispatches to a Worker; on LLM
return the main window opens the review screen."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class Stage1Page(QWidget):
    extract_requested = Signal(str, str)  # (rfi_uuid, revision_id-or-empty)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Extract Requirements from an RFI")
        title.setProperty("role", "section")
        layout.addWidget(title)
        hint = QLabel(
            "Enter the RFI document's Istari UUID, then extract its "
            "requirements schema with an LLM job."
        )
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(8)
        self.uuid_edit = QLineEdit()
        self.uuid_edit.setPlaceholderText("Istari UUID of the RFI file")
        self.revision_edit = QLineEdit()
        self.revision_edit.setPlaceholderText("Optional — uses the latest revision if empty")
        form.addRow("RFI Document UUID", self.uuid_edit)
        form.addRow("Revision (optional)", self.revision_edit)
        layout.addLayout(form)

        self.extract_button = QPushButton("Extract Requirements")
        self.extract_button.setObjectName("primaryButton")
        self.extract_button.clicked.connect(self._on_extract)
        layout.addWidget(self.extract_button)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

    def _on_extract(self) -> None:
        rfi_uuid = self.uuid_edit.text().strip()
        if not rfi_uuid:
            self.status_label.setText("Enter an RFI UUID first.")
            return
        self.extract_requested.emit(rfi_uuid, self.revision_edit.text().strip())

    def set_busy(self, busy: bool) -> None:
        self.extract_button.setEnabled(not busy)

    def show_progress(self, state: str, detail: str) -> None:
        self.status_label.setText(f"[{state}] {detail}")
