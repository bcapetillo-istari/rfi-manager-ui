"""Configuration: ``config.toml`` plus environment-variable overrides (PRD §3.3).

Secrets (Istari token, LLM API key) come from environment variables ONLY and
are never read from or written to disk by the app.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Env vars. Non-secret settings may also be overridden via env for enclave use.
ENV_ISTARI_TOKEN = "ISTARI_TOKEN"
ENV_ISTARI_BASE_URL = "ISTARI_BASE_URL"
ENV_LLM_API_KEY = "RFI_LLM_API_KEY"
ENV_LLM_PROVIDER = "RFI_LLM_PROVIDER"
ENV_LLM_MODEL = "RFI_LLM_MODEL"
ENV_LLM_ENDPOINT = "RFI_LLM_ENDPOINT"


@dataclass(frozen=True)
class IstariConfig:
    base_url: str
    token: str  # from env only
    request_timeout_s: float = 60.0
    job_poll_interval_s: float = 3.0
    job_timeout_s: float = 900.0
    retries: int = 2


@dataclass(frozen=True)
class LLMConfig:
    provider: str  # "anthropic" | "openai_compatible"
    model: str
    api_key: str  # from env only
    endpoint: str | None = None  # openai_compatible only
    request_timeout_s: float = 300.0
    retries: int = 2
    max_tokens: int = 8192


@dataclass(frozen=True)
class AppConfig:
    istari: IstariConfig
    llm: LLMConfig


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def load_config(path: str | Path = "config.toml") -> AppConfig:
    """Load ``config.toml``, apply env overrides, and pull secrets from env."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"{path} not found — copy config.example.toml to config.toml and edit it"
        )
    with path.open("rb") as f:
        raw = tomllib.load(f)

    istari_raw = raw.get("istari", {})
    llm_raw = raw.get("llm", {})

    token = os.environ.get(ENV_ISTARI_TOKEN, "")
    if not token:
        raise ConfigError(f"{ENV_ISTARI_TOKEN} environment variable is not set")
    if "token" in istari_raw:
        raise ConfigError(
            "istari.token must NOT appear in config.toml — set ISTARI_TOKEN in the environment"
        )

    base_url = os.environ.get(ENV_ISTARI_BASE_URL) or istari_raw.get("base_url", "")
    if not base_url:
        raise ConfigError("istari.base_url missing from config.toml (or ISTARI_BASE_URL env)")

    api_key = os.environ.get(ENV_LLM_API_KEY, "")
    if not api_key:
        raise ConfigError(f"{ENV_LLM_API_KEY} environment variable is not set")
    if "api_key" in llm_raw or "key" in llm_raw:
        raise ConfigError(
            "llm.api_key must NOT appear in config.toml — set RFI_LLM_API_KEY in the environment"
        )

    provider = os.environ.get(ENV_LLM_PROVIDER) or llm_raw.get("provider", "anthropic")
    if provider not in ("anthropic", "openai_compatible"):
        raise ConfigError(f"unknown llm.provider: {provider!r}")
    model = os.environ.get(ENV_LLM_MODEL) or llm_raw.get("model", "")
    if not model:
        raise ConfigError("llm.model missing from config.toml (or RFI_LLM_MODEL env)")
    endpoint = os.environ.get(ENV_LLM_ENDPOINT) or llm_raw.get("endpoint")
    if provider == "openai_compatible" and not endpoint:
        raise ConfigError("llm.endpoint is required when llm.provider = 'openai_compatible'")

    return AppConfig(
        istari=IstariConfig(
            base_url=base_url,
            token=token,
            request_timeout_s=float(istari_raw.get("request_timeout_s", 60.0)),
            job_poll_interval_s=float(istari_raw.get("job_poll_interval_s", 3.0)),
            job_timeout_s=float(istari_raw.get("job_timeout_s", 900.0)),
            retries=int(istari_raw.get("retries", 2)),
        ),
        llm=LLMConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            endpoint=endpoint,
            request_timeout_s=float(llm_raw.get("request_timeout_s", 300.0)),
            retries=int(llm_raw.get("retries", 2)),
            max_tokens=int(llm_raw.get("max_tokens", 8192)),
        ),
    )
