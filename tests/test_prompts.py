"""T3 — prompt generation: Prompt B carries every committed requirement id and
its constraints; changing the schema changes the prompt version stamp."""

from __future__ import annotations

from rfi_manager.models import Requirement
from rfi_manager.prompts import (
    PROMPT_VERSION,
    prompt_a,
    prompt_b,
    prompt_b_version,
    with_retry_errors,
)

REQS = [
    Requirement(id="C-01", label="MOSA", description="MOSA compliance level",
                type="enum", options=["Compliant", "Partial", "Non-compliant"]),
    Requirement(id="C-02", label="Weight (kg)", description="Unit weight",
                type="numeric", unit="kg"),
    Requirement(id="C-03", label="ITAR", description="ITAR restricted?",
                type="boolean", required=True),
]


def test_prompt_b_contains_every_requirement_id_and_constraints():
    p = prompt_b(REQS, "RESPONSE TEXT")
    for r in REQS:
        assert f'"{r.id}"' in p
        assert r.description in p
    assert '"Compliant"' in p and '"Non-compliant"' in p  # enum options
    assert '"kg"' in p  # numeric unit
    assert "RESPONSE TEXT" in p


def test_prompt_a_contains_rfi_text_and_contract_keys():
    p = prompt_a("THE RFI TEXT")
    assert "THE RFI TEXT" in p
    for key in ("id", "label", "description", "type", "unit", "options", "required"):
        assert f'"{key}"' in p


def test_prompt_version_stamp_changes_with_schema():
    v1 = prompt_b_version("1.0")
    v2 = prompt_b_version("1.1")
    assert v1 != v2
    assert PROMPT_VERSION in v1 and PROMPT_VERSION in v2
    assert "1.0" in v1 and "1.1" in v2


def test_retry_suffix_appends_errors():
    p = with_retry_errors("BASE", ["error one", "error two"])
    assert p.startswith("BASE")
    assert "- error one" in p and "- error two" in p
