"""Fake adapters for tests (PRD §7). No network, no credentials, ever."""

from __future__ import annotations


class FakeLLM:
    """Scripted LLM: returns queued responses in order and records calls."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []  # (system, user)
        self.model = "fake-llm"

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self._responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        return self._responses.pop(0)
