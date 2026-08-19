"""pdf_extraction.extract_text: page joining and None-safe handling of pages
pdfplumber can't extract text from."""

from __future__ import annotations

import pdfplumber

from rfi_manager import pdf_extraction


class _FakePage:
    def __init__(self, text: str | None) -> None:
        self._text = text

    def extract_text(self) -> str | None:
        return self._text


class _FakePdf:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages

    def __enter__(self) -> "_FakePdf":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_extract_text_joins_pages(monkeypatch):
    fake_pdf = _FakePdf([_FakePage("page one"), _FakePage("page two")])
    monkeypatch.setattr(pdfplumber, "open", lambda _stream: fake_pdf)

    assert pdf_extraction.extract_text(b"irrelevant") == "page one\n\npage two"


def test_extract_text_handles_unextractable_page(monkeypatch):
    """A scanned/image-only page returns None from extract_text() — must not
    crash the join, just contribute an empty page."""
    fake_pdf = _FakePdf([_FakePage("page one"), _FakePage(None)])
    monkeypatch.setattr(pdfplumber, "open", lambda _stream: fake_pdf)

    assert pdf_extraction.extract_text(b"irrelevant") == "page one\n\n"
