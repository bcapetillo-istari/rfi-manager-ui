"""LLM adapters (PRD §3.4). ALL LLM calls live here — nothing else makes
HTTP calls or imports provider SDKs.

Interface: ``complete(system: str, user: str) -> str``. Two implementations:
``AnthropicLLM`` (official ``anthropic`` SDK, messages API) and
``OpenAICompatibleLLM`` (generic chat-completions URL via httpx, covers most
gov/enclave endpoints). Selection via config (``llm.provider``).
"""

from __future__ import annotations

import httpx

from .config import LLMConfig


class LLMError(Exception):
    """Raised when an LLM call fails or returns an unusable response."""


class AnthropicLLM:
    """Anthropic messages API via the official ``anthropic`` SDK.

    Wraps ``client.messages.create`` (non-streaming; max_tokens stays below
    the SDK's non-streaming timeout guard). The SDK retries 429/5xx itself
    per ``max_retries``.
    """

    def __init__(self, config: LLMConfig) -> None:
        import anthropic  # deferred so tests never need the SDK installed

        self._config = config
        self._client = anthropic.Anthropic(
            api_key=config.api_key,
            timeout=config.request_timeout_s,
            max_retries=config.retries,
        )
        self.model = config.model

    def complete(self, system: str, user: str) -> str:
        """One-shot completion: ``client.messages.create`` with a system
        prompt and a single user message; returns concatenated text blocks."""
        import anthropic

        try:
            response = self._client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APIError as e:
            raise LLMError(f"Anthropic API error: {e}") from e
        if response.stop_reason == "refusal":
            raise LLMError("Anthropic API declined the request (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise LLMError(
                "LLM output truncated at max_tokens — raise llm.max_tokens in config"
            )
        text = "".join(b.text for b in response.content if b.type == "text")
        if not text:
            raise LLMError("Anthropic API returned no text content")
        return text


class OpenAICompatibleLLM:
    """Generic OpenAI-compatible chat-completions endpoint via httpx.

    Wraps ``POST {endpoint}`` with the standard ``{"model", "messages",
    "max_tokens"}`` payload and a Bearer token. Retries on 429/5xx up to
    ``config.retries`` times.
    """

    def __init__(self, config: LLMConfig) -> None:
        if not config.endpoint:
            raise ValueError("openai_compatible provider requires llm.endpoint")
        self._config = config
        self.model = config.model

    def complete(self, system: str, user: str) -> str:
        """One-shot chat completion; returns the first choice's message content."""
        payload = {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        last_error: Exception | None = None
        for _attempt in range(self._config.retries + 1):
            try:
                resp = httpx.post(
                    self._config.endpoint,  # type: ignore[arg-type]
                    json=payload,
                    headers=headers,
                    timeout=self._config.request_timeout_s,
                )
            except httpx.HTTPError as e:
                last_error = e
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = LLMError(f"LLM endpoint returned HTTP {resp.status_code}")
                continue
            if resp.status_code != 200:
                raise LLMError(
                    f"LLM endpoint returned HTTP {resp.status_code}: {resp.text[:500]}"
                )
            try:
                content = resp.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError) as e:
                raise LLMError(f"unexpected chat-completions response shape: {e}") from e
            if not isinstance(content, str) or not content:
                raise LLMError("LLM endpoint returned empty content")
            return content
        raise LLMError(f"LLM call failed after retries: {last_error}")


def make_llm(config: LLMConfig) -> AnthropicLLM | OpenAICompatibleLLM:
    """Instantiate the configured LLM adapter (config.provider)."""
    if config.provider == "anthropic":
        return AnthropicLLM(config)
    return OpenAICompatibleLLM(config)
