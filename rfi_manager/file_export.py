"""Comparison table export (FR8) — CSV today; XLSX/HTML can follow the same
shape. Consumes exactly what the Qt table renders (pipeline.ComparisonRow),
so the export always matches what's on screen."""

from __future__ import annotations

import csv
from pathlib import Path

from .models import Requirement
from .pipeline import ComparisonRow


def export_comparison_csv(
    requirements: list[Requirement],
    rows: list[ComparisonRow],
    path: str | Path,
) -> None:
    """Write the comparison table to ``path`` as CSV: Vendor, one column per
    requirement (display value only — quote/page/confidence stay in the
    detail pane, not the export), then schema version and stale flag."""
    header = (
        ["Vendor"]
        + [f"{req.id} — {req.label}" for req in requirements]
        + ["Schema version", "Stale"]
    )
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            values = [row.cells[req.id].display() for req in requirements]
            writer.writerow(
                [row.vendor, *values, row.schema_version or "", "yes" if row.stale else "no"]
            )
