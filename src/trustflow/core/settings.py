"""Application settings. Secrets come only from the environment."""

import os

from pydantic import Field, field_validator, model_validator
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
    # Retained as an ignored constructor option for old clients. Never used.
    llm_api_key: str | None = Field(default=None, exclude=True, repr=False)
    llm_base_url: str = "https://api.meta.ai/v1"
    planning_model: str = "muse-wonder-1.0"
    review_model: str = "muse-wonder-1.0"
    fast_model: str = "muse-spark-1.3"
    # Bounded live-call behaviour. Raw httpx is used (no SDK), so this is the
    # only retry layer: total attempts = 1 + llm_max_retries, no multiplication.
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    llm_backoff_seconds: float = 0.5
    llm_max_requests: int = Field(default=24, ge=1, le=1000)
    llm_max_cost_usd: float = Field(default=0.50, gt=0, le=100, allow_inf_nan=False)
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
    # M3 persistence (docs/m3.md). Unset keeps the exact M1/M2
    # in-memory behaviour; set (demo/Docker) for durable SQLite state.
    db_path: str | None = None
    # M3 approval authorization. When set, approval/recovery endpoints
    # require the X-Approval-Token header. Never logged or persisted.
    approval_token: str | None = None
    # Local demo UI origins allowed by CORS.
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    @model_validator(mode="after")
    def _live_bounds(self) -> "Settings":
        if self.llm_mode == "live":
            models = {"muse-wonder-1.0", "muse-spark-1.3"}
            if self.planning_model not in models or self.review_model not in models:
                raise ValueError("Unsupported live planning/review model ID")
            if not 0 <= self.llm_max_retries <= 3:
                raise ValueError("Live retries must be between 0 and 3")
            if not 0 < self.llm_timeout_seconds <= 120:
                raise ValueError("Live timeout must be in (0, 120] seconds")
            if not 0 <= self.llm_backoff_seconds <= 5:
                raise ValueError("Live backoff must be between 0 and 5 seconds")
            if not 16 <= self.max_output_tokens <= 16384:
                raise ValueError("Live max_output_tokens must be between 16 and 16384")
        return self

    @property
    def model_api_key(self) -> str | None:
        """Only the process environment MODEL_API_KEY authorizes inference."""
        return os.environ.get("MODEL_API_KEY") or None


def get_settings() -> Settings:
    return Settings()
