"""Prompt templates (FR9).

Prompt A extracts requirements from RFI text. Prompt B extracts answers from a
response and is GENERATED from the committed requirements so the two cannot
drift. ``PROMPT_VERSION`` is stamped into every uploaded artifact's metadata.
"""

from __future__ import annotations

import json

from .models import Requirement

PROMPT_VERSION = "A1-B1"

SYSTEM_PROMPT = (
    "You are a precise document-extraction engine. You respond with a single "
    "JSON document and nothing else: no prose, no markdown fences, no comments."
)

_PROMPT_A_TEMPLATE = """\
Below is the full text of a Request for Information (RFI) document.

Extract every requirement a vendor is asked to address, as a JSON array. Each
element must have exactly these keys:

  "id":          the RFI's own numbering when present (e.g. "3.2.1", "C-01");
                 otherwise invent sequential ids "R-01", "R-02", ...
  "label":       a column heading of at most 4 words, e.g. "Unit weight (kg)"
  "description": the requirement as stated in the RFI, condensed
  "type":        one of "boolean", "numeric", "enum", "text"
  "unit":        measurement unit string for numeric requirements, else null
  "options":     for enum requirements, the array of allowed values, else null
  "required":    true if the RFI marks it mandatory (shall/must), else false

Rules:
- Every id must be unique.
- "enum" requires a non-null "options" array; "numeric" should name a "unit".
- Output ONLY the JSON array.

RFI TEXT:
---
{rfi_text}
---
"""

_PROMPT_B_HEADER = """\
Below are (1) the requirement schema extracted from an RFI and (2) the full
text of one vendor's RFI response document.

For EVERY requirement in the schema, find the vendor's answer in the response
text. Output a JSON array with exactly one element per requirement id, each
with exactly these keys:

  "id":         the requirement id, copied exactly from the schema
  "value":      the answer, typed per the requirement:
                  boolean -> true or false
                  numeric -> a JSON number (convert to the requirement's unit)
                  enum    -> exactly one string from the requirement's options
                  text    -> a short string
                If the response does not address the requirement, the string
                "NOT_FOUND".
  "unit":       the requirement's unit for numeric answers, else null
  "quote":      the verbatim sentence from the response that supports the
                value ("" when NOT_FOUND)
  "page":       integer page number the quote appears on, or null if unknown
  "confidence": "high", "medium", or "low"; must be "none" when NOT_FOUND

Rules:
- One element per schema requirement id: no omissions, no duplicates, no
  extra ids.
- Never guess: if unsure, use NOT_FOUND rather than inventing a value.
- Output ONLY the JSON array.

REQUIREMENT SCHEMA (id, description, type, unit, options):
{schema_block}

RESPONSE TEXT:
---
{response_text}
---
"""

_RETRY_SUFFIX = """\

Your previous output failed validation with these errors:
{errors}

Produce a corrected JSON array that fixes every error. Output ONLY the JSON array.
"""


def prompt_a(rfi_text: str) -> str:
    """Prompt A: requirements extraction from RFI text."""
    return _PROMPT_A_TEMPLATE.format(rfi_text=rfi_text)


def prompt_b(requirements: list[Requirement], response_text: str) -> str:
    """Prompt B: answers extraction, generated from the committed requirements."""
    schema_lines = []
    for r in requirements:
        entry = {
            "id": r.id,
            "description": r.description,
            "type": r.type,
            "unit": r.unit,
            "options": r.options,
        }
        schema_lines.append(json.dumps(entry, ensure_ascii=False))
    schema_block = "\n".join(schema_lines)
    return _PROMPT_B_HEADER.format(schema_block=schema_block, response_text=response_text)


def with_retry_errors(prompt: str, errors: list[str]) -> str:
    """Append validation errors for the single retry pass (PRD §4)."""
    return prompt + _RETRY_SUFFIX.format(errors="\n".join(f"- {e}" for e in errors))
