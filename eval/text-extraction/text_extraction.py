"""Stage 1 eval — text-extraction completeness (EVAL_FRAMEWORK_PRD §3.1).

BOTH production extraction paths run against the golden PDF and must satisfy
the same completeness properties (requirement IDs literal, golden quotes
findable, no corruption markers):

- custom — ``rfi_manager.pdf_extraction.extract_text``, the exact function
  the pipeline calls under DO_CUSTOM_EXTRACTION.
- istari (marker: live) — a real ``@istari:extract`` job submitted through
  ``IstariAdapter.submit_extraction_job`` and polled with the production
  ``wait_for_job`` loop. Function/tool/OS names come from istari_adapter's
  constants — the eval never restates them, so it always measures the
  shipped configuration.

The golden rfi-answers.json doubles as the pointer to the platform copy of
the same PDF (response_uuid/response_revision); an integrity eval asserts
the platform bytes still match the repo golden so a drifted platform copy
cannot masquerade as an extraction regression.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from rfi_manager import pdf_extraction
from rfi_manager.pipeline import wait_for_job

GOLDEN_DIR = Path(__file__).parent / "golden"
GOLDEN_PDF = GOLDEN_DIR / "RFI_Response_A_Meridian_Aerosystems.pdf"
GOLDEN_ANSWERS = json.loads(
    (GOLDEN_DIR / "rfi-answers.json").read_text(encoding="utf-8")
)

EXPECTATIONS = json.loads(
    (GOLDEN_DIR / "expectations.json").read_text(encoding="utf-8")
)
_KNOWN_ABSENT = set(EXPECTATIONS.get("known_absent_ids", []))

# ids known absent from THIS document (see expectations.json) are strict
# xfails: the eval stays green while they're absent, and goes red if the
# document/extraction ever starts producing them (golden drift alarm)
ANSWER_IDS = [
    (
        pytest.param(
            a["id"],
            marks=pytest.mark.xfail(
                reason="id absent from this document per expectations.json",
                strict=True,
            ),
        )
        if a["id"] in _KNOWN_ABSENT
        else a["id"]
    )
    for a in GOLDEN_ANSWERS["answers"]
]
QUOTES = [(a["id"], a["quote"]) for a in GOLDEN_ANSWERS["answers"] if a["quote"]]

# "Findable as a substring" tolerates LAYOUT differences between
# extractors, never content differences: whitespace collapses and
# typographic punctuation folds to ASCII, but every content character must
# be present.
_WS = re.compile(r"\s+")
_FOLD = str.maketrans(
    {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "}
)


def _normalize(s: str) -> str:
    return _WS.sub(" ", s.translate(_FOLD)).strip()


# ------------------------------------------------------------ extractions


@pytest.fixture(scope="session")
def custom_text() -> str:
    """Production custom-extraction path, on the repo golden bytes."""
    return pdf_extraction.extract_text(GOLDEN_PDF.read_bytes())


@pytest.fixture(scope="session")
def istari_text(istari) -> str:
    """Production Istari-extraction path: real job against the platform copy
    of the golden PDF, production polling, production artifact read."""
    model_id = GOLDEN_ANSWERS["response_uuid"]
    job_id = istari.submit_extraction_job(model_id)
    wait_for_job(istari, job_id)
    return istari.get_extracted_text(model_id)


@pytest.fixture(
    scope="session",
    params=["custom", pytest.param("istari", marks=pytest.mark.live)],
)
def extracted(request) -> tuple[str, str]:
    """(mode, extracted_text) — every completeness property below runs once
    per production extraction path; extraction itself runs once per mode."""
    return request.param, request.getfixturevalue(f"{request.param}_text")


# -------------------------------------------------------------- integrity


@pytest.mark.live
def test_platform_copy_matches_golden_pdf(istari):
    """The platform model the istari mode runs against must be byte-identical
    to the repo golden — otherwise quote failures would be misattributed."""
    remote = istari.read_revision_bytes(GOLDEN_ANSWERS["response_revision"])
    assert (
        hashlib.sha256(remote).hexdigest()
        == hashlib.sha256(GOLDEN_PDF.read_bytes()).hexdigest()
    ), "platform copy of the golden PDF differs from the repo golden. Not able to run eval as input files are not identical."


# ------------------------------------------------ completeness properties


def test_extraction_not_empty_no_corruption(extracted):
    """Non-empty output, no replacement-character mojibake."""
    mode, text = extracted
    assert text.strip(), f"[{mode}] extraction produced no text"
    assert "�" not in text, f"[{mode}] replacement characters in extraction"


@pytest.mark.parametrize("answer_id", ANSWER_IDS)
def test_requirement_id_literal(extracted, answer_id):
    """Every requirement ID appears as a literal string."""
    mode, text = extracted
    assert (
        answer_id in text
    ), f"[{mode}] requirement id {answer_id!r} not literal in extracted text"


@pytest.mark.parametrize("answer_id,quote", QUOTES, ids=[i for i, _ in QUOTES])
def test_golden_quote_findable(extracted, answer_id, quote):
    """Every golden quote is findable as a normalized substring."""
    mode, text = extracted
    # single-line message: pytest's short-summary section only shows the
    # first line, and the missing quote is the diagnostic
    assert _normalize(quote) in _normalize(
        text
    ), f"[{mode}] quote for {answer_id} not findable: {quote!r}"
