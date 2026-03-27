"""Anthropic API wrapper — reusable across all reviewers."""
from __future__ import annotations

import json
import logging

import anthropic

from config import settings

logger = logging.getLogger(__name__)


class LLMClient:
    """Thin wrapper around the Anthropic SDK with structured JSON output helpers."""

    def __init__(self) -> None:
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.llm_model
        self._max_tokens = settings.llm_max_tokens

    def complete(self, system: str, user: str) -> str:
        """Return raw text completion."""
        message = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text

    def complete_json(self, system: str, user: str) -> dict:
        """
        Return parsed JSON from a completion.

        Expects the model to respond with a JSON object. Strips markdown code
        fences if the model wraps the output.
        """
        raw = self.complete(system=system, user=user)
        cleaned = _strip_code_fences(raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error("LLM returned non-JSON output: %s", raw[:500])
            raise ValueError(f"LLM did not return valid JSON: {exc}") from exc


def _strip_code_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` wrappers if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop first and last fence lines
        inner = lines[1:] if lines[0].startswith("```") else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        return "\n".join(inner).strip()
    return text
