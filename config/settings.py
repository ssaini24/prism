from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # GitHub — personal token (fallback) or App credentials (preferred)
    github_webhook_secret: str = ""
    github_token: str = ""          # PAT — used only if App credentials are not set
    github_app_id: int = 0          # GitHub App ID
    github_app_private_key: str = ""  # Contents of the .pem file (newlines as \n)

    # LLM — set LLM_PROVIDER to "openai" or "anthropic"
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_max_tokens: int = 2048

    # Provider credentials (only the one matching LLM_PROVIDER is required)
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # Feature flags
    enable_code_review: bool = False
    enable_orm_review: bool = False
    enable_db_analysis_via_mcp: bool = False

    # App
    log_level: str = "INFO"
    environment: str = "development"


settings = Settings()
