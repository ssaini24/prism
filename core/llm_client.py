"""LLM client with pluggable provider support (Anthropic, OpenAI)."""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

from config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class LLMClient(ABC):
    """Common interface all LLM provider clients must implement."""

    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Return raw text completion."""

    def complete_json(self, system: str, user: str) -> dict:
        """
        Return parsed JSON from a completion.

        Strips markdown code fences if the model wraps the output.
        """
        raw = self.complete(system=system, user=user)
        cleaned = _strip_code_fences(raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error("LLM returned non-JSON output: %s", raw[:500])
            raise ValueError(f"LLM did not return valid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------


class AnthropicClient(LLMClient):
    """Anthropic Claude via the official SDK."""

    def __init__(self) -> None:
        import anthropic
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.llm_model
        self._max_tokens = settings.llm_max_tokens

    def complete(self, system: str, user: str) -> str:
        message = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text


# ---------------------------------------------------------------------------
# OpenAI provider
# ---------------------------------------------------------------------------


class OpenAIClient(LLMClient):
    """OpenAI GPT via the official SDK."""

    def __init__(self) -> None:
        from openai import OpenAI
        self._client = OpenAI(api_key=settings.openai_api_key)
        self._model = settings.llm_model
        self._max_tokens = settings.llm_max_tokens

    def complete(self, system: str, user: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, type[LLMClient]] = {
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
}


def create_llm_client() -> LLMClient:
    """Instantiate the provider configured via LLM_PROVIDER env var."""
    provider = settings.llm_provider.lower()
    cls = _PROVIDERS.get(provider)
    if cls is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{provider}'. Choose from: {list(_PROVIDERS)}"
        )
    logger.info("Using LLM provider: %s / model: %s", provider, settings.llm_model)
    return cls()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_code_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` wrappers if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:] if lines[0].startswith("```") else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        return "\n".join(inner).strip()
    return text
