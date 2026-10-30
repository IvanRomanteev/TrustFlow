"""Application settings. Secrets come only from the environment."""

import os

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_LLM_MODES = frozenset({"fake", "live"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRUSTFLOW_", env_file=".env", extra="ignore")

    env: str = "dev"
    # fake = deterministic offline (tests/demo). live = Meta Model API.
    # Live failures never fall back to fake; they fail closed.
    llm_mode: str = "fake"

    @field_validator("llm_mode", mode="before")
    @classmethod
    def _validate_llm_mode(cls, value: object) -> str:
        """Explicit contract: fake|live (any case). Anything else is an error,
        never a silent Fake selection."""
        normalized = str(value).strip().lower()
        if normalized not in SUPPORTED_LLM_MODES:
            raise ValueError(
                f"Unsupported TRUSTFLOW_LLM_MODE={value!r}; expected one of "
                f"{sorted(SUPPORTED_LLM_MODES)}"
            )
        return normalized
    llm_api_key: str | None = None  # legacy TRUSTFLOW_LLM_API_KEY fallback only
    llm_base_url: str = "https://api.meta.ai/v1"
    planning_model: str = "muse-wonder-1.0"
    review_model: str = "muse-wonder-1.0"
    fast_model: str = "muse-spark-1.3"
    # Bounded live-call behaviour. Raw httpx is used (no SDK), so this is the
    # only retry layer: total attempts = 1 + llm_max_retries, no multiplication.
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    llm_backoff_seconds: float = 0.5
    max_output_tokens: int = 1200
    token_budget_per_workflow: int = 20000
    # Total semantic-review attempts per workflow, INCLUDING the initial call.
    semantic_max_attempts: int = 3
    # M2 retrieval (advisory, deterministic, offline; see docs/m2.md).
    retrieval_top_k: int = 3
    retrieval_min_score: float = 0.1
    high_risk_threshold: float = 1000.0
    max_steps: int = 20
    max_retries: int = 2
    step_timeout_seconds: float = 5.0

    @property
    def model_api_key(self) -> str | None:
        """TrustFlow expects MODEL_API_KEY; TRUSTFLOW_LLM_API_KEY is a fallback.

        Muse may inject META_API_KEY for its own use; TrustFlow does not
        read it implicitly. The value is never logged or persisted.
        """
        return os.environ.get("MODEL_API_KEY") or self.llm_api_key


def get_settings() -> Settings:
    return Settings()
