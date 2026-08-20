"""Comparison table export (FR8): three artifacts uploaded together to the
RFI model —

  answers_tidy.json — the record: full provenance header, status enum,
    honest nulls (a missing answer is JSON null, never a display string),
    one row per (requirement, vendor) pair, completeness invariant enforced
    at build.
  answers.csv        — wide/pivot shape, same as the Qt table and the HTML
    report: one row per vendor, one column per requirement. Display values
    only — quote/page/confidence/status live in answers_tidy.json, not
    here, since they don't fit this shape.
  review.html         — the human review surface: a spreadsheet-style
    vendor x requirement grid (sticky vendor column, horizontal scroll),
    quote/page/confidence/status on hover, status-driven cell tinting, and
    a provenance footer.

Consumes exactly what the Qt table renders (pipeline.ComparisonRow), so
every export always matches what's on screen.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from html import escape
from typing import Any

from .models import Requirement
from .pipeline import ComparisonCell, ComparisonRow, PipelineError
from .istari_adapter import IstariAdapter


class ExportName(Enum):
    COMPARISON_CSV = "answers.csv"
    TIDY_ANSWERS_JSON = "answers_tidy.json"
    REVIEW_HTML = "review.html"


@dataclass
class ExportItem:
    name: ExportName
    data: str | dict[str, Any]


class AnswerStatus(str, Enum):
    """Cell-level answer quality — independent of ComparisonRow.stale, which
    is a row-level "answered against an older schema" flag."""

    NOT_FOUND = "not_found"
    HIGH_CONFIDENCE = "high_confidence"
    MEDIUM_CONFIDENCE = "medium_confidence"
    LOW_CONFIDENCE = "low_confidence"
    NO_CONFIDENCE = "no_confidence"


_STATUS_BY_CONFIDENCE = {
    "high": AnswerStatus.HIGH_CONFIDENCE,
    "medium": AnswerStatus.MEDIUM_CONFIDENCE,
    "low": AnswerStatus.LOW_CONFIDENCE,
    "none": AnswerStatus.NO_CONFIDENCE,
}


def _cell_status(cell: ComparisonCell) -> AnswerStatus:
    if cell.is_not_found:
        return AnswerStatus.NOT_FOUND
    return _STATUS_BY_CONFIDENCE.get(cell.confidence, AnswerStatus.NO_CONFIDENCE)


def _check_complete(requirements: list[Requirement], rows: list[ComparisonRow]) -> None:
    """Every response row must carry a cell for every requirement.
    build_comparison_rows already guarantees this (it fills a None-value
    cell for any missing answer), but it's re-checked here so a caller
    passing hand-built rows fails loudly instead of silently exporting a
    gap."""
    for row in rows:
        missing = [req.id for req in requirements if req.id not in row.cells]
        if missing:
            raise PipelineError(
                f"response {row.response_uuid}: missing cell(s) for requirement "
                f"id(s) {', '.join(missing)} — refusing to export incomplete data"
            )


def _tidy_rows(
    requirements: list[Requirement], rows: list[ComparisonRow]
) -> list[dict[str, Any]]:
    """One dict per (requirement, response) pair — the long/tidy shape
    answers_tidy.json is derived from.

    Honest nulls: a cell with no real answer (NOT_FOUND, or simply absent)
    is exported as JSON null in ``value`` — never the display string ("—")
    and never the raw "NOT_FOUND" sentinel. The ``status`` field already
    carries why the value is null.
    """
    _check_complete(requirements, rows)
    tidy: list[dict[str, Any]] = []
    for row in rows:
        for req in requirements:
            cell = row.cells[req.id]
            status = _cell_status(cell)
            tidy.append(
                {
                    "requirement_id": req.id,
                    "requirement_label": req.label,
                    "vendor": row.vendor,
                    "response_uuid": row.response_uuid,
                    "response_revision": row.response_revision,
                    "answers_artifact_uuid": row.answers_artifact_uuid,
                    "schema_version": row.schema_version,
                    "stale": row.stale,
                    "status": status.value,
                    "value": None if cell.is_not_found else cell.value,
                    "unit": cell.unit,
                    "quote": cell.quote or None,
                    "page": cell.page,
                    "confidence": cell.confidence,
                }
            )
    return tidy


def build_tidy_answers_json(
    requirements: list[Requirement], rows: list[ComparisonRow]
) -> ExportItem:
    """The record. ``data`` is a plain dict (not a JSON string) —
    upload_json_artifact/istari_adapter does its own json.dumps."""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requirements": [
            {"id": req.id, "label": req.label, "type": req.type} for req in requirements
        ],
        "rows": _tidy_rows(requirements, rows),
    }
    return ExportItem(ExportName.TIDY_ANSWERS_JSON, payload)


def build_comparison_csv(
    requirements: list[Requirement], rows: list[ComparisonRow]
) -> ExportItem:
    """Wide/pivot shape, matching the Qt table and the HTML report: one row
    per vendor, one column per requirement. Display values only — full
    provenance lives in answers_tidy.json instead."""
    _check_complete(requirements, rows)
    header = (
        ["Vendor"]
        + [f"{req.id} — {req.label}" for req in requirements]
        + ["Schema version", "Stale"]
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        values = [row.cells[req.id].display() for req in requirements]
        writer.writerow(
            [
                row.vendor,
                *values,
                row.schema_version or "",
                "yes" if row.stale else "no",
            ]
        )
    return ExportItem(ExportName.COMPARISON_CSV, buffer.getvalue())


_STATUS_COLOR = {
    AnswerStatus.NOT_FOUND: "#f8d7da",
    AnswerStatus.LOW_CONFIDENCE: "#ffe5cc",
    AnswerStatus.MEDIUM_CONFIDENCE: "#fff3cd",
    AnswerStatus.HIGH_CONFIDENCE: "#ffffff",
    AnswerStatus.NO_CONFIDENCE: "#ececec",
}

_STATUS_LABEL = {
    AnswerStatus.NOT_FOUND: "Not found",
    AnswerStatus.LOW_CONFIDENCE: "Low confidence",
    AnswerStatus.MEDIUM_CONFIDENCE: "Medium confidence",
    AnswerStatus.HIGH_CONFIDENCE: "High confidence",
    AnswerStatus.NO_CONFIDENCE: "No confidence reported",
}


def _cell_td(req: Requirement, row: ComparisonRow) -> str:
    cell = row.cells[req.id]
    status = _cell_status(cell)
    value = escape(cell.display() + (f" {cell.unit}" if cell.unit else ""))
    tooltip_lines = [f"Status: {_STATUS_LABEL[status]}"]
    if cell.quote:
        tooltip_lines.append(f"Quote: “{cell.quote}”")
    if cell.page is not None:
        tooltip_lines.append(f"Page: {cell.page}")
    tooltip_lines.append(f"Confidence: {cell.confidence}")
    tooltip = escape("\n".join(tooltip_lines))
    return (
        f'<td class="cell" style="background:{_STATUS_COLOR[status]}" '
        f'data-tooltip="{tooltip}">{value}</td>'
    )


def build_html_report(
    requirements: list[Requirement], rows: list[ComparisonRow]
) -> ExportItem:
    """The human review surface: a spreadsheet-style vendor x requirement
    grid (sticky vendor column, horizontal scroll for wide schemas), with
    quote/page/confidence/status shown in a fixed-position overlay on hover
    (a few lines of inline vanilla JS — the horizontal-scroll wrapper's
    overflow-x:auto clips a plain CSS :hover/absolute tooltip; see the
    script's own comment) and a provenance footer for auditing."""
    generated_at = datetime.now(timezone.utc).isoformat()

    header_cells = "".join(
        f"<th>{escape(req.id)} — {escape(req.label)}</th>" for req in requirements
    )
    body_rows = "".join(
        "<tr>"
        + f'<td class="vendor-col cell" data-tooltip="{escape("Response UUID: " + row.response_uuid)}">'
        + f"{escape(row.vendor)}{' ⚠ stale' if row.stale else ''}</td>"
        + "".join(_cell_td(req, row) for req in requirements)
        + "</tr>"
        for row in rows
    )
    footer_rows = "".join(
        "<tr>"
        f"<td>{escape(row.vendor)}</td>"
        f"<td>{escape(row.response_uuid)}</td>"
        f"<td>{escape(row.response_revision or '—')}</td>"
        f"<td>{escape(row.answers_artifact_uuid or '—')}</td>"
        f"<td>{escape(row.schema_version or '—')}</td>"
        f"<td>{'yes' if row.stale else 'no'}</td>"
        "</tr>"
        for row in rows
    )

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>RFI Response Comparison</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Arial, sans-serif; margin: 24px; color: #222; }}
  h1 {{ font-size: 18px; }}
  h2 {{ font-size: 14px; }}
  .meta {{ color: #666; font-size: 12px; margin-bottom: 16px; }}
  .table-wrap {{ overflow-x: auto; border: 1px solid #ccc; max-width: 100%; }}
  table {{ border-collapse: collapse; font-size: 13px; white-space: nowrap; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; }}
  th {{ background: #f2f2f2; position: sticky; top: 0; z-index: 1; }}
  .vendor-col {{
    position: sticky; left: 0; background: #fafafa; font-weight: 600;
    z-index: 2; border-right: 2px solid #bbb;
  }}
  thead th.vendor-col {{ z-index: 3; }}
  footer table {{ white-space: normal; }}
  .cell {{ cursor: default; }}
  #tooltip {{
    position: fixed;
    display: none;
    max-width: 320px;
    background: #222;
    color: #fff;
    padding: 8px 10px;
    border-radius: 4px;
    font-size: 12px;
    line-height: 1.4;
    white-space: pre-line;
    box-shadow: 0 2px 8px rgba(0,0,0,0.35);
    z-index: 1000;
    pointer-events: none;
  }}
</style>
</head>
<body>
<h1>RFI Response Comparison</h1>
<div class="meta">
  Generated {escape(generated_at)} — {len(rows)} response(s), {len(requirements)} requirement(s).
  Hover a cell for quote / page / confidence / status.
</div>
<div class="table-wrap">
<table>
<thead><tr><th class="vendor-col">Vendor</th>{header_cells}</tr></thead>
<tbody>
{body_rows}
</tbody>
</table>
</div>
<footer>
<h2>Provenance</h2>
<table>
<thead><tr><th>Vendor</th><th>Response UUID</th><th>Response revision</th>
<th>Answers artifact UUID</th><th>Schema version</th><th>Stale</th></tr></thead>
<tbody>
{footer_rows}
</tbody>
</table>
</footer>
<div id="tooltip"></div>
<script>
// The table wrapper needs overflow-x:auto for horizontal scroll, which per
// the CSS spec forces overflow-y to compute as auto too — so a plain
// position:absolute tooltip anchored inside a <td> gets clipped by that
// scroll container. position:fixed (viewport-relative) sidesteps it, hence
// the few lines of vanilla JS instead of pure CSS :hover.
(function () {{
  var tip = document.getElementById("tooltip");
  document.querySelectorAll("td[data-tooltip]").forEach(function (td) {{
    td.addEventListener("mouseenter", function () {{
      tip.textContent = td.getAttribute("data-tooltip");
      tip.style.display = "block";
      var r = td.getBoundingClientRect();
      var top = r.bottom + 6;
      var left = r.left;
      tip.style.top = top + "px";
      tip.style.left = left + "px";
      var tr = tip.getBoundingClientRect();
      if (tr.right > window.innerWidth - 8) {{
        tip.style.left = Math.max(8, window.innerWidth - tr.width - 8) + "px";
      }}
      if (tr.bottom > window.innerHeight - 8) {{
        tip.style.top = Math.max(8, r.top - tr.height - 6) + "px";
      }}
    }});
    td.addEventListener("mouseleave", function () {{
      tip.style.display = "none";
    }});
  }});
}})();
</script>
</body>
</html>
"""
    return ExportItem(ExportName.REVIEW_HTML, html_doc)


def upload_exports(
    istari: IstariAdapter, model_id: str, exports: list[ExportItem]
) -> None:
    for export in exports:
        if export.name in (ExportName.COMPARISON_CSV, ExportName.REVIEW_HTML):
            istari.upload_text_artifact(
                model_id=model_id, name=export.name.value, text=export.data
            )
        elif export.name == ExportName.TIDY_ANSWERS_JSON:
            istari.upload_json_artifact(
                model_id=model_id, name=export.name.value, payload=export.data
            )
