"""Code review agent — reviews general code quality, logic, and reliability."""
from __future__ import annotations

import logging
import os

from core.llm_client import LLMClient, create_llm_client
from models.review import ExtractedQuery, Issue, ReviewResult
from reviewers.base_reviewer import BaseReviewer
from reviewers.code_review import prompts

logger = logging.getLogger(__name__)

# File extensions this reviewer handles
_SUPPORTED_EXTENSIONS = {
    ".go", ".py", ".php", ".rb", ".js", ".ts",
    ".java", ".cs", ".rs", ".cpp", ".c", ".kt", ".swift",
}

# Map extension to language name for prompt context
_LANGUAGE_MAP = {
    ".go": "go", ".py": "python", ".php": "php", ".rb": "ruby",
    ".js": "javascript", ".ts": "typescript", ".java": "java",
    ".cs": "csharp", ".rs": "rust", ".cpp": "cpp",
    ".c": "c", ".kt": "kotlin", ".swift": "swift",
}

# Skip files that are purely config or generated
_SKIP_PATTERNS = {"_test.", "vendor/", "node_modules/", ".pb.go", "_generated"}


class CodeReviewAgent(BaseReviewer):
    """Reviews code changes for logic bugs, error handling, complexity, and reliability."""

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self._llm = llm_client or create_llm_client()

    @property
    def name(self) -> str:
        return "Code Review Agent"

    def can_review(self, query: ExtractedQuery) -> bool:
        if query.suppressed:
            return False
        ext = os.path.splitext(query.file)[1].lower()
        if ext not in _SUPPORTED_EXTENSIONS:
            return False
        # Skip generated/vendor/test files
        if any(pattern in query.file for pattern in _SKIP_PATTERNS):
            return False
        return True

    def review(self, query: ExtractedQuery, schema_context: str = "") -> ReviewResult:
        ext = os.path.splitext(query.file)[1].lower()
        language = _LANGUAGE_MAP.get(ext, "code")

        try:
            return self._llm_review(query.raw, language)
        except Exception as exc:
            logger.warning("Code review LLM call failed for %s:%d — %s", query.file, query.line, exc)
            return ReviewResult(explanation="Code review unavailable due to LLM error.")

    def _llm_review(self, code: str, language: str) -> ReviewResult:
        user_prompt = prompts.build_user_prompt(code, language)
        raw = self._llm.complete_json(
            system=prompts.SYSTEM_PROMPT,
            user=user_prompt,
        )
        return _parse_response(raw)


def _parse_response(data: dict) -> ReviewResult:
    issues = []
    for item in data.get("issues", []):
        try:
            issues.append(Issue(**item))
        except Exception:
            pass
    return ReviewResult(
        issues=issues,
        explanation=data.get("explanation", ""),
        suppressed=data.get("suppressed", []),
    )
