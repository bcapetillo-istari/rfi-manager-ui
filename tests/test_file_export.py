"""file_export.export_comparison_csv: correct CSV shape and escaping of
free-text fields (descriptions/answers routinely contain commas/quotes)."""

from __future__ import annotations

import csv
from pathlib import Path

from rfi_manager.file_export import export_comparison_csv
from rfi_manager.models import Requirement
from rfi_manager.pipeline import ComparisonCell, ComparisonRow

REQUIREMENTS = [
    Requirement(id="C-01", label="MOSA, Compliance", description="d", type="enum",
                options=["Compliant"]),
    Requirement(id="C-02", label="Weight (kg)", description="d", type="numeric", unit="kg"),
]


def make_row(vendor: str, values: dict[str, object], schema_version="1.0", stale=False):
    return ComparisonRow(
        vendor=vendor, response_uuid="resp-1", response_revision="rev-1",
        answers_artifact_uuid="art-1", schema_version=schema_version, stale=stale,
        cells={
            req.id: ComparisonCell(value=values.get(req.id))
            for req in REQUIREMENTS
        },
    )


def test_export_writes_header_and_rows(tmp_path: Path):
    rows = [
        make_row("Acme, Inc.", {"C-01": "Compliant", "C-02": 38.5}),
        make_row("Contoso", {"C-01": None, "C-02": 12}, schema_version="1.1", stale=True),
    ]
    out = tmp_path / "export.csv"

    export_comparison_csv(REQUIREMENTS, rows, out)

    with out.open(newline="", encoding="utf-8") as f:
        parsed = list(csv.reader(f))

    assert parsed[0] == ["Vendor", "C-01 — MOSA, Compliance", "C-02 — Weight (kg)",
                          "Schema version", "Stale"]
    # a vendor name containing a comma round-trips as one field, not two
    assert parsed[1] == ["Acme, Inc.", "Compliant", "38.5", "1.0", "no"]
    assert parsed[2] == ["Contoso", "—", "12", "1.1", "yes"]
