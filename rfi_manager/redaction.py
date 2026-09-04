"""Secret redaction: the single choke point for scrubbing credentials out of
logs, error dialogs, and the on-disk ``.rfiproj`` (via ``record.error``).

Scrubs registered exact values (the PAT) and structural patterns (Bearer
headers, ``token=``/``api_key=`` pairs, presigned-URL signatures), while
leaving ordinary diagnostic text intact. No package imports, so it's safe to
import anywhere.
"""

from __future__ import annotations

import re

_PLACEHOLDER = "***REDACTED***"

# Exact secret values to scrub wherever they appear (the PAT, at runtime).
_secrets: set[str] = set()

_PATTERNS = [
    # Authorization: Bearer <token>  /  "Bearer eyJ..."
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"),
    # token=... / api_key: ... / authorization=...  (value up to a delimiter)
    re.compile(r"(?i)\b(?:api[_-]?key|token|authorization|secret)\b\s*[=:]\s*[^\s,;&'\"}\)]+"),
    # presigned-URL signature params (AWS SigV4 & friends): scrub the value
    re.compile(
        r"(?i)([?&](?:X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token|"
        r"Signature|AWSAccessKeyId)=)[^&\s]+"
    ),
]


def register_secret(value: str | None) -> None:
    """Register an exact secret (the PAT) to scrub everywhere. No-op for
    empty/short values so a trivial string can't redact unrelated text."""
    if value and len(value) >= 8:
        _secrets.add(value)


def clear_secrets() -> None:
    """Drop all registered secrets (test isolation; a full disconnect)."""
    _secrets.clear()


def redact(text: str) -> str:
    """Return ``text`` with every registered secret and known credential
    pattern replaced by a placeholder. Safe on any string; leaves ordinary
    text untouched."""
    if not text:
        return text
    for secret in _secrets:
        text = text.replace(secret, _PLACEHOLDER)
    for pattern in _PATTERNS:
        # keep the captured prefix (e.g. "?X-Amz-Signature=") when present
        text = pattern.sub(
            lambda m: (m.group(1) if m.groups() else "") + _PLACEHOLDER, text
        )
    return text
