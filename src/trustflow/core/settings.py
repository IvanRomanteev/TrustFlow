"""Application settings. Secrets come only from the environment."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRUSTFLOW_", env_file=".env", extra="ignore")

    env: str = "dev"
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.meta.example/v1"
    planning_model: str = "muse-wonder-1.0"
    fast_model: str = "muse-spark-1.3"
    high_risk_threshold: float = 1000.0
    max_steps: int = 20
    max_retries: int = 2
    step_timeout_seconds: float = 5.0


def get_settings() -> Settings:
    return Settings()
