"""file_export builders: tidy-row shape, honest nulls, status enum mapping,
completeness invariant, and CSV/HTML escaping of free-text fields."""

from __future__ import annotations

import csv
import io

import pytest

from rfi_manager.file_export import (
    AnswerStatus,
    ExportName,
    build_comparison_csv,
    build_html_report,
    build_tidy_answers_json,
)
from rfi_manager.models import Requirement
from rfi_manager.pipeline import ComparisonCell, ComparisonRow, PipelineError

REQUIREMENTS = [
    Requirement(id="C-01", label="MOSA, Compliance", description="d", type="enum",
                options=["Compliant"]),
    Requirement(id="C-02", label="Weight (kg)", description="d", type="numeric", unit="kg"),
]


def make_row(vendor: str, cells: dict[str, ComparisonCell], schema_version="1.0", stale=False):
    return ComparisonRow(
        vendor=vendor, response_uuid="resp-1", response_revision="rev-1",
        answers_artifact_uuid="art-1", schema_version=schema_version, stale=stale,
        cells=cells,
    )


def test_tidy_json_has_honest_nulls_and_status(tmp_path):
    row = make_row("Acme, Inc.", {
        "C-01": ComparisonCell(value="Compliant", confidence="high", quote="We comply.", page=3),
        "C-02": ComparisonCell(value=None),  # no answer at all
    })

    item = build_tidy_answers_json(REQUIREMENTS, [row])

    assert item.name is ExportName.TIDY_ANSWERS_JSON
    payload = item.data
    assert isinstance(payload, dict)
    assert payload["requirements"] == [
        {"id": "C-01", "label": "MOSA, Compliance", "type": "enum"},
        {"id": "C-02", "label": "Weight (kg)", "type": "numeric"},
    ]
    rows = payload["rows"]
    assert len(rows) == 2  # one per (requirement, response) pair

    found = rows[0]
    assert found["status"] == AnswerStatus.HIGH_CONFIDENCE.value
    assert found["value"] == "Compliant"
    assert found["quote"] == "We comply."

    not_found = rows[1]
    assert not_found["status"] == AnswerStatus.NOT_FOUND.value
    assert not_found["value"] is None  # honest null, not "—" and not "NOT_FOUND"
    assert not_found["quote"] is None  # empty string normalized to null too


def test_tidy_rejects_incomplete_row():
    incomplete_row = make_row("Acme", {"C-01": ComparisonCell(value="Compliant")})  # missing C-02

    with pytest.raises(PipelineError, match="C-02"):
        build_tidy_answers_json(REQUIREMENTS, [incomplete_row])


def test_csv_is_wide_one_row_per_vendor():
    rows = [
        make_row("Acme, Inc.", {
            "C-01": ComparisonCell(value="Compliant", confidence="high"),
            "C-02": ComparisonCell(value=38.5, unit="kg", confidence="medium"),
        }),
        make_row("Contoso", {
            "C-01": ComparisonCell(value=None),
            "C-02": ComparisonCell(value=12, unit="kg", confidence="low"),
        }, schema_version="0.9", stale=True),
    ]

    csv_item = build_comparison_csv(REQUIREMENTS, rows)

    parsed = list(csv.reader(io.StringIO(csv_item.data)))
    assert parsed[0] == ["Vendor", "C-01 — MOSA, Compliance", "C-02 — Weight (kg)",
                          "Schema version", "Stale"]
    # a vendor name containing a comma round-trips as one field, not two
    assert parsed[1] == ["Acme, Inc.", "Compliant", "38.5", "1.0", "no"]
    assert parsed[2] == ["Contoso", "—", "12", "0.9", "yes"]
    assert len(parsed) == 3  # header + one row per vendor, not per (req, vendor)


def test_html_report_escapes_and_tints_by_status():
    row = make_row("<script>alert(1)</script>", {
        "C-01": ComparisonCell(value="Compliant", confidence="high"),
        "C-02": ComparisonCell(value=None),
    })

    item = build_html_report(REQUIREMENTS, [row])

    assert item.name is ExportName.REVIEW_HTML
    assert "<script>alert(1)</script>" not in item.data  # escaped, not injected
    assert "&lt;script&gt;" in item.data
    assert 'class="vendor-col"' in item.data  # sticky column present
    assert "Status: Not found" in item.data  # hover tooltip content


# ------------------------------------------- T/O compliance report artifacts

TO_REQUIREMENTS = [
    Requirement(id="1.1", label="Range", description="d", type="numeric",
                unit="km", threshold=200, objective=1500, direction="at_least",
                gradeable=True),
    Requirement(id="1.9", label="Comms", description="d", type="text",
                gradeable=True),
]


def to_row(vendor="Acme", range_value=250, range_unit="km", llm_grade=None):
    return ComparisonRow(
        vendor=vendor, response_uuid="resp-1", response_revision="rev-1",
        answers_artifact_uuid="art-1", schema_version="1.1", stale=False,
        cells={
            "1.1": ComparisonCell(value=range_value, unit=range_unit,
                                   quote="Range is stated.", page=2,
                                   confidence="high"),
            "1.9": ComparisonCell(value="Secure BLOS with FMV", confidence="medium",
                                   quote="We provide BLOS SATCOM.", page=4,
                                   llm_grade=llm_grade,
                                   llm_grade_rationale="BLOS meets O." if llm_grade else None),
        },
    )


def test_compliance_report_json_grades_and_provenance():
    from rfi_manager.file_export import build_compliance_report_json

    item = build_compliance_report_json(TO_REQUIREMENTS, [to_row(llm_grade="MEETS_OBJECTIVE")])

    assert item.name is ExportName.COMPLIANCE_JSON
    rows = item.data["rows"]
    assert len(rows) == 2

    numeric = next(r for r in rows if r["requirement_id"] == "1.1")
    assert numeric["grade"] == "MEETS_THRESHOLD"  # 250 km: >= T, < O
    assert numeric["grade_source"] == "deterministic"
    assert numeric["threshold"] == 200 and numeric["objective"] == 1500

    text = next(r for r in rows if r["requirement_id"] == "1.9")
    assert text["grade"] == "MEETS_OBJECTIVE"
    assert text["grade_source"] == "llm"
    assert text["llm_grade_rationale"] == "BLOS meets O."


def test_compliance_report_json_conversion_audit_trail():
    from rfi_manager.file_export import build_compliance_report_json

    item = build_compliance_report_json(TO_REQUIREMENTS, [to_row(range_value=120, range_unit="nmi")])
    numeric = next(r for r in item.data["rows"] if r["requirement_id"] == "1.1")
    assert numeric["original_value"] == 120
    assert numeric["original_unit"] == "nmi"
    assert round(numeric["converted_value"], 2) == 222.24
    assert numeric["converted_unit"] == "km"
    assert numeric["grade"] == "MEETS_THRESHOLD"


def test_compliance_report_html_colors_by_grade():
    from rfi_manager.file_export import build_compliance_report_html, _GRADE_COLOR

    item = build_compliance_report_html(
        TO_REQUIREMENTS, [to_row(range_value=100)]  # 100 km < T=200
    )
    assert item.name is ExportName.COMPLIANCE_HTML
    assert _GRADE_COLOR["BELOW_THRESHOLD"] in item.data
    assert "Grade: BELOW_THRESHOLD (deterministic)" in item.data
    # text cell had no llm_grade -> grey NOT_GRADEABLE
    assert "Grade: NOT_GRADEABLE" in item.data
    assert "legend" in item.data
