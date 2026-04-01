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
        _log_usage(self._model, message.usage.input_tokens, message.usage.output_tokens)
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
        usage = response.usage
        if usage:
            _log_usage(self._model, usage.prompt_tokens, usage.completion_tokens)
        return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------

# Approximate cost per 1K tokens (input / output) by model
_COST_PER_1K: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001":  (0.00025, 0.00125),
    "claude-sonnet-4-6":          (0.003,   0.015),
    "gpt-4o-mini":                (0.00015, 0.0006),
    "gpt-4o":                     (0.0025,  0.01),
}


def _log_usage(model: str, input_tokens: int, output_tokens: int, estimated: bool = False) -> None:
    cost_in, cost_out = _COST_PER_1K.get(model, (0.0, 0.0))
    total_cost = (input_tokens / 1000 * cost_in) + (output_tokens / 1000 * cost_out)
    label = "~" if estimated else ""
    logger.info(
        "LLM usage — model: %s | in: %d tokens | out: %d tokens | cost: %s$%.6f%s",
        model, input_tokens, output_tokens, label, total_cost, " (estimated)" if estimated else "",
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Claude Code CLI provider (uses local `claude` binary — no API key needed)
# ---------------------------------------------------------------------------


class ClaudeCodeClient(LLMClient):
    """
    Calls the local Claude Code CLI via subprocess.

    Uses your existing `claude` auth — no ANTHROPIC_API_KEY required.
    Set LLM_PROVIDER=claude-code in .env to use this.
    """

    def complete(self, system: str, user: str) -> str:
        import subprocess
        prompt = f"{system}\n\n{user}"
        # Estimate tokens: ~4 chars per token
        est_input_tokens = len(prompt) // 4
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude Code CLI error: {result.stderr.strip()}")
        output = result.stdout.strip()
        est_output_tokens = len(output) // 4
        # claude-code uses haiku-class model — estimate cost at haiku rates
        _log_usage("claude-haiku-4-5-20251001", est_input_tokens, est_output_tokens, estimated=True)
        return output


_PROVIDERS: dict[str, type[LLMClient]] = {
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
    "claude-code": ClaudeCodeClient,
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
