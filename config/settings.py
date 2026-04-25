from __future__ import annotations

import os


class Settings:
    # GitHub — personal token (fallback) or App credentials (preferred)
    github_webhook_secret: str = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    github_token: str = os.environ.get("GITHUB_TOKEN", "")  # PAT — used only if App credentials are not set
    github_app_id: int = int(os.environ.get("GITHUB_APP_ID", "0"))  # GitHub App ID
    github_app_private_key: str = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")  # Contents of the .pem file (newlines as \n)

    # LLM — set LLM_PROVIDER to "claude-code" or "anthropic"
    llm_provider: str = os.environ.get("LLM_PROVIDER", "claude-code")
    llm_model: str = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
    llm_max_tokens: int = int(os.environ.get("LLM_MAX_TOKENS", "2048"))

    # Provider credentials (only the one matching LLM_PROVIDER is required)
    openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")

    # App
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")
    environment: str = os.environ.get("ENVIRONMENT", "development")


settings = Settings()
