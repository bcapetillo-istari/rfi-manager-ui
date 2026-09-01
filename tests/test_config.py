"""Env-driven configuration: RESPONSE_EXTRACTION_BATCH_SIZE and the
DO_CUSTOM_EXTRACTION flag (rfi_manager/config.py)."""

from __future__ import annotations

import pytest

from rfi_manager.config import (
    ENV_DO_CUSTOM_EXTRACTION,
    ENV_RESPONSE_EXTRACTION_BATCH_SIZE,
    ConfigError,
    custom_extraction_enabled,
    load_response_extraction_batch_size,
)


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """These tests control the environment directly — the developer's local
    .env must not leak into them via the helpers' load_dotenv()."""
    monkeypatch.setattr("rfi_manager.config.load_dotenv", lambda *a, **k: None)


def test_batch_size_unset_is_none(monkeypatch):
    monkeypatch.delenv(ENV_RESPONSE_EXTRACTION_BATCH_SIZE, raising=False)
    assert load_response_extraction_batch_size() is None


def test_batch_size_empty_is_none(monkeypatch):
    monkeypatch.setenv(ENV_RESPONSE_EXTRACTION_BATCH_SIZE, "  ")
    assert load_response_extraction_batch_size() is None


def test_batch_size_valid(monkeypatch):
    monkeypatch.setenv(ENV_RESPONSE_EXTRACTION_BATCH_SIZE, "20")
    assert load_response_extraction_batch_size() == 20


def test_batch_size_garbage_raises(monkeypatch):
    """A typo'd value must fail loudly, never silently fall back."""
    monkeypatch.setenv(ENV_RESPONSE_EXTRACTION_BATCH_SIZE, "twenty")
    with pytest.raises(ConfigError, match="positive integer"):
        load_response_extraction_batch_size()


def test_batch_size_below_one_raises(monkeypatch):
    monkeypatch.setenv(ENV_RESPONSE_EXTRACTION_BATCH_SIZE, "0")
    with pytest.raises(ConfigError, match=">= 1"):
        load_response_extraction_batch_size()


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False),
])
def test_custom_extraction_flag(monkeypatch, raw, expected):
    monkeypatch.setenv(ENV_DO_CUSTOM_EXTRACTION, raw)
    assert custom_extraction_enabled() is expected
