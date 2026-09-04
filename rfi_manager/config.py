"""Configuration: ``config.toml`` plus environment-variable overrides (PRD §3.3).

A local ``.env`` file is loaded first (git-ignored, developer convenience).
The only client-side secret is the Istari token (env var only). There is no
LLM API key: LLM credentials are Istari Linked Accounts, selected in the UI
and bound to jobs by credential id (docs/LLM_Call_Flow.md).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Env vars. Non-secret settings may also be overridden via env for enclave use.
ENV_ISTARI_TOKEN = "ISTARI_TOKEN"
ENV_ISTARI_BASE_URL = "ISTARI_BASE_URL"
ENV_LLM_PROVIDER = "RFI_LLM_PROVIDER"
ENV_LLM_MODEL = "RFI_LLM_MODEL"
ENV_DO_CUSTOM_EXTRACTION = "DO_CUSTOM_EXTRACTION"
ENV_RESPONSE_EXTRACTION_BATCH_SIZE = "RESPONSE_EXTRACTION_BATCH_SIZE"
ENV_LOG_FILE_LOCATION = "LOG_FILE_LOCATION"

_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def custom_extraction_enabled() -> bool:
    """DO_CUSTOM_EXTRACTION feature flag (env-only, unset/false by default):
    when true, PDF text extraction runs locally via pdfplumber
    (rfi_manager/pdf_extraction.py) instead of Istari's own @istari:extract
    job."""
    load_dotenv()
    return (
        os.environ.get(ENV_DO_CUSTOM_EXTRACTION, "").strip().lower()
        in _TRUTHY_ENV_VALUES
    )


def load_log_file_location() -> str | None:
    """LOG_FILE_LOCATION (env): log directory; unset -> None (platform default)."""
    load_dotenv()
    value = os.environ.get(ENV_LOG_FILE_LOCATION, "").strip()
    return value or None


def load_response_extraction_batch_size() -> int | None:
    """RESPONSE_EXTRACTION_BATCH_SIZE (env, loaded like DO_CUSTOM_EXTRACTION):
    how many responses process in flight at once (rolling window across the
    platform's agents; 1 = sequential). Unset/empty -> None, letting
    config.toml's response_concurrency or the built-in default (20) apply.
    Set but invalid -> ConfigError — a typo'd value must never silently
    degrade to a default."""
    load_dotenv()
    raw = os.environ.get(ENV_RESPONSE_EXTRACTION_BATCH_SIZE, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"{ENV_RESPONSE_EXTRACTION_BATCH_SIZE} must be a positive "
            f"integer, got {raw!r}"
        ) from None
    if value < 1:
        raise ConfigError(
            f"{ENV_RESPONSE_EXTRACTION_BATCH_SIZE} must be >= 1, got {value}"
        )
    return value


@dataclass(frozen=True)
class IstariConfig:
    base_url: str
    token: str  # from env only
    request_timeout_s: float = 60.0
    job_poll_interval_s: float = 3.0
    job_timeout_s: float = 900.0
    retries: int = 2
    # responses processed in flight at once (rolling window across the
    # platform's agents); 1 = sequential
    response_concurrency: int = 20


@dataclass(frozen=True)
class LLMConfig:
    """Defaults forwarded as LLM-job parameters; empty means the deployed
    @istari_utils:rfi_manager module decides."""

    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class AppConfig:
    istari: IstariConfig
    llm: LLMConfig
    do_custom_extraction: bool = False


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def load_config(
    path: str | Path = "config.toml", *, require_token: bool = True
) -> AppConfig:
    """Load ``config.toml``, apply env overrides, and pull secrets from env.

    With ``require_token=False`` (the UI flow: registry URL and PAT come from
    the connection bar), a missing ISTARI_TOKEN yields ``token=""`` — the env
    var, when set, is only a prefill for the PAT box.
    """
    load_dotenv()
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"{path} not found — copy config.example.toml to config.toml and edit it"
        )
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e

    istari_raw = raw.get("istari", {})
    llm_raw = raw.get("llm", {})

    token = os.environ.get(ENV_ISTARI_TOKEN, "")
    if not token and require_token:
        raise ConfigError(f"{ENV_ISTARI_TOKEN} environment variable is not set")
    if "token" in istari_raw:
        raise ConfigError(
            "istari.token must NOT appear in config.toml — set ISTARI_TOKEN in the environment"
        )
    if "api_key" in llm_raw or "key" in llm_raw or "endpoint" in llm_raw:
        raise ConfigError(
            "LLM keys/endpoints do not belong in config.toml — LLM calls run as "
            "Istari jobs and credentials are Linked Accounts selected in the UI"
        )

    base_url = os.environ.get(ENV_ISTARI_BASE_URL) or istari_raw.get("base_url", "")
    if not base_url and require_token:
        raise ConfigError(
            "istari.base_url missing from config.toml (or ISTARI_BASE_URL env)"
        )

    return AppConfig(
        istari=IstariConfig(
            base_url=base_url,
            token=token,
            request_timeout_s=float(istari_raw.get("request_timeout_s", 60.0)),
            job_poll_interval_s=float(istari_raw.get("job_poll_interval_s", 3.0)),
            job_timeout_s=float(istari_raw.get("job_timeout_s", 900.0)),
            retries=int(istari_raw.get("retries", 2)),
            # env wins over config.toml, matching the other env overrides
            response_concurrency=(
                load_response_extraction_batch_size()
                or int(istari_raw.get("response_concurrency", 20))
            ),
        ),
        llm=LLMConfig(
            provider=os.environ.get(ENV_LLM_PROVIDER) or llm_raw.get("provider"),
            model=os.environ.get(ENV_LLM_MODEL) or llm_raw.get("model"),
        ),
        do_custom_extraction=custom_extraction_enabled(),
    )
