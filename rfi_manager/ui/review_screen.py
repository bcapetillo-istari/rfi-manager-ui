"""Requirements review screen (FR2): editable table of requirements with
add/delete/reorder, inline validation (duplicate ids, enum without options,
numeric without unit warning), user-settable schema_version, and a
"Commit to Istari" button. Pure view logic — commit is dispatched by the
main window."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models import REQUIREMENT_TYPES, Requirement

_COLUMNS = ["id", "label", "description", "type", "unit", "options", "required"]
_COL = {name: i for i, name in enumerate(_COLUMNS)}


class ReviewScreen(QWidget):
    commit_requested = Signal(list, str)  # (list[Requirement], schema_version)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._busy = False  # a commit is in flight; edits must not re-enable it
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Review Extracted Requirements")
        title.setProperty("role", "section")
        layout.addWidget(title)
        hint = QLabel(
            "Edit, add, or remove rows as needed, then commit the schema "
            "to Istari."
        )
        hint.setProperty("role", "hint")
        layout.addWidget(hint)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(lambda _item: self._revalidate())
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        for label, handler in [
            ("Add Row", self._add_row),
            ("Delete Row", self._delete_row),
            ("Move Up", lambda: self._move_row(-1)),
            ("Move Down", lambda: self._move_row(1)),
        ]:
            b = QPushButton(label)
            b.clicked.connect(handler)
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(QLabel("Schema Version"))
        self.schema_edit = QLineEdit("1.0")
        self.schema_edit.setMaximumWidth(80)
        self.schema_edit.textChanged.connect(lambda _t: self._revalidate())
        buttons.addWidget(self.schema_edit)
        self.commit_button = QPushButton("Commit to Istari")
        self.commit_button.setObjectName("primaryButton")
        self.commit_button.clicked.connect(self._on_commit)
        buttons.addWidget(self.commit_button)
        layout.addLayout(buttons)

        self.validation_label = QLabel("")
        self.validation_label.setWordWrap(True)
        layout.addWidget(self.validation_label)

    # ------------------------------------------------------------- loading

    def load(self, requirements: list[Requirement], schema_version: str = "1.0") -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for req in requirements:
            self._append_row(req)
        self.table.blockSignals(False)
        self.schema_edit.setText(schema_version)
        self._revalidate()

    def _append_row(self, req: Requirement | None = None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = {
            "id": req.id if req else "",
            "label": req.label if req else "",
            "description": req.description if req else "",
            "unit": (req.unit or "") if req else "",
            # '|'-separated: enum options may legally contain commas
            "options": " | ".join(req.options or []) if req else "",
        }
        for name, value in values.items():
            self.table.setItem(row, _COL[name], QTableWidgetItem(value))
        combo = QComboBox()
        combo.addItems(REQUIREMENT_TYPES)
        combo.setCurrentText(req.type if req else "text")
        combo.currentTextChanged.connect(lambda _t: self._revalidate())
        self.table.setCellWidget(row, _COL["type"], combo)
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        check.setCheckState(
            Qt.CheckState.Checked if (req and req.required) else Qt.CheckState.Unchecked
        )
        self.table.setItem(row, _COL["required"], check)

    # ------------------------------------------------------------- editing

    def _add_row(self) -> None:
        self.table.blockSignals(True)
        self._append_row()
        self.table.blockSignals(False)
        self._revalidate()

    def _delete_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self._revalidate()

    def _move_row(self, delta: int) -> None:
        row = self.table.currentRow()
        target = row + delta
        if row < 0 or not (0 <= target < self.table.rowCount()):
            return
        reqs = self.requirements()
        reqs[row], reqs[target] = reqs[target], reqs[row]
        self.load(reqs, self.schema_edit.text())
        self.table.setCurrentCell(target, 0)

    # ---------------------------------------------------------- validation

    def requirements(self) -> list[Requirement]:
        """Current table contents as Requirements (unvalidated)."""
        reqs: list[Requirement] = []
        for row in range(self.table.rowCount()):
            def text(col: str) -> str:
                item = self.table.item(row, _COL[col])
                return item.text().strip() if item else ""

            combo = self.table.cellWidget(row, _COL["type"])
            options = [o.strip() for o in text("options").split("|") if o.strip()]
            required_item = self.table.item(row, _COL["required"])
            reqs.append(
                Requirement(
                    id=text("id"),
                    label=text("label"),
                    description=text("description"),
                    type=combo.currentText() if combo else "text",
                    unit=text("unit") or None,
                    options=options or None,
                    required=bool(
                        required_item
                        and required_item.checkState() is Qt.CheckState.Checked
                    ),
                )
            )
        return reqs

    def validate(self) -> tuple[list[str], list[str]]:
        """Inline validation per FR2. Returns (errors, warnings)."""
        errors: list[str] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for i, req in enumerate(self.requirements()):
            where = f"row {i + 1}"
            if not req.id:
                errors.append(f"{where}: empty id")
            elif req.id in seen:
                errors.append(f"{where}: duplicate id '{req.id}'")
            else:
                seen.add(req.id)
            if not req.label:
                errors.append(f"{where}: empty label")
            if not req.description:
                errors.append(f"{where}: empty description")
            if req.type == "enum" and not req.options:
                errors.append(f"{where}: enum without options")
            if req.type == "numeric" and not req.unit:
                warnings.append(f"{where}: numeric without unit")
        if not self.schema_edit.text().strip():
            errors.append("schema_version is empty")
        return errors, warnings

    def _revalidate(self) -> None:
        errors, warnings = self.validate()
        parts = [f"✗ {e}" for e in errors] + [f"⚠ {w}" for w in warnings]
        self.validation_label.setText("\n".join(parts))
        self.commit_button.setEnabled(not errors and not self._busy)

    def _on_commit(self) -> None:
        errors, _warnings = self.validate()
        if errors or self._busy:
            return  # button should be disabled; never commit invalid data
        self.commit_requested.emit(self.requirements(), self.schema_edit.text().strip())

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._revalidate()
