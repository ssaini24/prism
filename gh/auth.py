"""GitHub authentication — GitHub App (preferred) or PAT fallback."""
from __future__ import annotations

import logging

from github import Github, GithubIntegration

from config import settings

logger = logging.getLogger(__name__)


def get_github_client(repo_full_name: str) -> Github:
    """
    Return an authenticated Github client.

    Uses a GitHub App installation token when GITHUB_APP_ID and
    GITHUB_APP_PRIVATE_KEY are configured; falls back to the PAT otherwise.
    """
    if settings.github_app_id and settings.github_app_private_key:
        return _app_client(repo_full_name)
    return Github(settings.github_token)


def get_token(repo_full_name: str) -> str:
    """Return a raw bearer token for direct requests calls."""
    if settings.github_app_id and settings.github_app_private_key:
        return _installation_token(repo_full_name)
    return settings.github_token


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _integration() -> GithubIntegration:
    private_key = settings.github_app_private_key.replace("\\n", "\n")
    return GithubIntegration(str(settings.github_app_id), private_key)


def _installation_token(repo_full_name: str) -> str:
    integration = _integration()
    owner = repo_full_name.split("/")[0]
    installation = integration.get_installation(owner, repo_full_name.split("/")[1])
    token = integration.get_access_token(installation.id)
    return token.token


def _app_client(repo_full_name: str) -> Github:
    token = _installation_token(repo_full_name)
    logger.debug("Authenticated as GitHub App for %s", repo_full_name)
    return Github(token)
