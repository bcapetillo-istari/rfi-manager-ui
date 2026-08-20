"""Comparison table (FR6) + row detail pane (FR7).

QTableView + QSortFilterProxyModel: columns are Vendor, one per requirement,
then provenance (response UUID short form, schema rev). Numeric-aware
sorting, global search, filters (all / has NOT_FOUND / has low-confidence /
stale schema). NOT_FOUND renders as flagged em-dash; low/medium confidence
cells are tinted.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..models import NOT_FOUND, Requirement
from ..pipeline import ComparisonRow

_TINT_LOW = QColor(255, 220, 200)  # low confidence
_TINT_MEDIUM = QColor(255, 245, 200)  # medium confidence
_TINT_STALE = QColor(225, 225, 235)
_RED = QColor(180, 30, 30)

FILTERS = ["all", "has NOT_FOUND", "has low-confidence", "stale schema"]


class ComparisonModel(QAbstractTableModel):
    """Rows = ComparisonRow; fixed leading Vendor column, one column per
    requirement, then provenance columns."""

    def __init__(self) -> None:
        super().__init__()
        self._requirements: list[Requirement] = []
        self._rows: list[ComparisonRow] = []

    def set_data(
        self, requirements: list[Requirement], rows: list[ComparisonRow]
    ) -> None:
        self.beginResetModel()
        self._requirements = requirements
        self._rows = rows
        self.endResetModel()

    def row_at(self, row: int) -> ComparisonRow | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    # Qt model interface -----------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._requirements) + 3

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (
            role != Qt.ItemDataRole.DisplayRole
            or orientation != Qt.Orientation.Horizontal
        ):
            return None
        if section == 0:
            return "Vendor"
        req_index = section - 1
        if req_index < len(self._requirements):
            return self._requirements[req_index].label
        return ["Response UUID", "Schema rev"][section - 1 - len(self._requirements)]

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole) -> Any:
        row = self._rows[index.row()]
        col = index.column()
        n_reqs = len(self._requirements)

        if role == Qt.ItemDataRole.DisplayRole or role == Qt.ItemDataRole.UserRole:
            if col == 0:
                value: Any = row.vendor
            elif col <= n_reqs:
                cell = row.cells[self._requirements[col - 1].id]
                if role == Qt.ItemDataRole.UserRole:
                    # numeric-aware sort key
                    value = (
                        cell.value
                        if isinstance(cell.value, (int, float))
                        and not isinstance(cell.value, bool)
                        else cell.display()
                    )
                else:
                    value = cell.display()
            elif col == n_reqs + 1:
                value = row.response_uuid_short
            else:
                value = (row.schema_version or "") + (" (stale)" if row.stale else "")
            return value

        if 1 <= col <= n_reqs:
            cell = row.cells[self._requirements[col - 1].id]
            if role == Qt.ItemDataRole.ForegroundRole and cell.is_not_found:
                return QBrush(_RED)
            if role == Qt.ItemDataRole.BackgroundRole:
                if cell.confidence == "low":
                    return QBrush(_TINT_LOW)
                if cell.confidence == "medium":
                    return QBrush(_TINT_MEDIUM)
        if role == Qt.ItemDataRole.BackgroundRole and row.stale and col > n_reqs:
            return QBrush(_TINT_STALE)
        if role == Qt.ItemDataRole.ToolTipRole and 1 <= col <= n_reqs:
            cell = row.cells[self._requirements[col - 1].id]
            if cell.quote:
                return f"“{cell.quote}” (p.{cell.page}, {cell.confidence})"
        return None


class ComparisonProxy(QSortFilterProxyModel):
    """Global search across all columns + completeness/flag filters (FR6),
    numeric-aware sorting via UserRole keys."""

    def __init__(self) -> None:
        super().__init__()
        self.search_text = ""
        self.filter_mode = "all"
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set_search(self, text: str) -> None:
        self.search_text = text.lower()
        self.invalidateFilter()

    def set_filter_mode(self, mode: str) -> None:
        self.filter_mode = mode
        self.invalidateFilter()

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        lv = left.data(Qt.ItemDataRole.UserRole)
        rv = right.data(Qt.ItemDataRole.UserRole)
        l_num = isinstance(lv, (int, float))
        r_num = isinstance(rv, (int, float))
        if l_num and r_num:
            return lv < rv
        if l_num != r_num:
            return l_num  # numbers sort before text (em-dashes sink)
        return str(lv) < str(rv)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model: ComparisonModel = self.sourceModel()  # type: ignore[assignment]
        row = model.row_at(source_row)
        if row is None:
            return False
        if self.filter_mode == "has NOT_FOUND" and not row.has_not_found:
            return False
        if self.filter_mode == "has low-confidence" and not row.has_low_confidence:
            return False
        if self.filter_mode == "stale schema" and not row.stale:
            return False
        if self.search_text:
            haystack = [row.vendor, row.response_uuid, row.schema_version or ""]
            haystack += [c.display() for c in row.cells.values()]
            haystack += [c.quote for c in row.cells.values()]
            if not any(self.search_text in h.lower() for h in haystack):
                return False
        return True


class ComparisonPage(QWidget):
    refresh_requested = Signal()
    commit_observations_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Vendor Response Comparison")
        title.setProperty("role", "section")
        layout.addWidget(title)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(QLabel("Search"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search vendor, answer, or quote text")
        controls.addWidget(self.search_edit)
        controls.addWidget(QLabel("Filter"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(FILTERS)
        controls.addWidget(self.filter_combo)
        controls.addStretch(1)
        self.commit_button = QPushButton("Commit to Istari")
        self.commit_button.clicked.connect(self.commit_observations_requested.emit)
        controls.addWidget(self.commit_button)
        layout.addLayout(controls)

        self.model = ComparisonModel()
        self.proxy = ComparisonProxy()
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("Select a row for details and provenance.")

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        self.search_edit.textChanged.connect(self.proxy.set_search)
        self.filter_combo.currentTextChanged.connect(self.proxy.set_filter_mode)
        self.table.selectionModel().selectionChanged.connect(self._on_selection)

        self._requirements: list[Requirement] = []
        self._rows: list[ComparisonRow] = []
        self.commit_button.setEnabled(False)

    def load(self, requirements: list[Requirement], rows: list[ComparisonRow]) -> None:
        self._requirements = requirements
        self._rows = rows
        self.model.set_data(requirements, rows)
        self.table.resizeColumnsToContents()
        # a model reset clears the selection without a selectionChanged
        # signal — drop the previous data set's detail pane too (FR7)
        self.detail.clear()
        self.commit_button.setEnabled(bool(rows))

    def current_data(self) -> tuple[list[Requirement], list[ComparisonRow]]:
        """The exact (requirements, rows) currently on screen — used by the
        CSV export so it matches what's rendered, not a re-fetch."""
        return self._requirements, self._rows

    # FR7: detail pane with copyable provenance UUIDs
    def _on_selection(self) -> None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            self.detail.clear()
            return
        source_index = self.proxy.mapToSource(indexes[0])
        row = self.model.row_at(source_index.row())
        if row is None:
            return
        lines = [
            f"Vendor: {row.vendor}",
            f"Response UUID: {row.response_uuid}",
            f"Response revision: {row.response_revision or '—'}",
            f"Answers artifact UUID: {row.answers_artifact_uuid or '—'}",
            f"Schema version: {row.schema_version or '—'}"
            + (" (STALE)" if row.stale else ""),
            "",
        ]
        for req in self._requirements:
            cell = row.cells[req.id]
            value = cell.display() + (f" {cell.unit}" if cell.unit else "")
            lines.append(f"[{req.id}] {req.label}: {value} ({cell.confidence})")
            if cell.quote:
                lines.append(f"    “{cell.quote}” (page {cell.page or '?'})")
        self.detail.setPlainText("\n".join(lines))
