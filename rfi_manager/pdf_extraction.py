"""Local PDF text extraction via pdfplumber — the DO_CUSTOM_EXTRACTION path
(pipeline.py), run entirely client-side instead of Istari's @istari:extract
job."""

from __future__ import annotations

import io

import pdfplumber


def extract_text(pdf_bytes: bytes) -> str:
    """Return the whole document as text, page by page. pdfplumber's default
    ``extract_text()`` lays out characters by position, so table cells come
    through as text in place rather than being dropped."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    return "\n\n".join(pages)
