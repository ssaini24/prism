"""DB query reviewer — entry point for the database query review module."""
from __future__ import annotations

import logging

from core.llm_client import LLMClient, create_llm_client
from models.review import CostAnalysis, ExtractedQuery, Issue, ReviewResult
from reviewers.base_reviewer import BaseReviewer
from reviewers.db_query import prompts, rules

logger = logging.getLogger(__name__)

# SQL file extensions and patterns we care about
_SQL_EXTENSIONS = {".sql", ".pgsql", ".mysql"}
_CODE_EXTENSIONS = {".py", ".rb", ".js", ".ts", ".java", ".go", ".cs"}

# Extensions owned by ORM reviewer — DB reviewer must not process these
_ORM_EXTENSIONS = {".php"}


class DBQueryReviewer(BaseReviewer):
    """Reviews SQL queries for performance issues and anti-patterns."""

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self._llm = llm_client or create_llm_client()
        self._repo = ""

    @property
    def name(self) -> str:
        return "DB Query Reviewer"

    def can_review(self, query: ExtractedQuery) -> bool:
        if query.suppressed:
            return False
        import os
        ext = os.path.splitext(query.file)[1].lower()
        # PHP files are owned by the ORM reviewer
        if ext in _ORM_EXTENSIONS:
            return False
        return ext in _SQL_EXTENSIONS | _CODE_EXTENSIONS or ext == ""

    def set_repo(self, repo: str) -> None:
        self._repo = repo

    def review(self, query: ExtractedQuery, schema_context: str = "") -> ReviewResult:
        if query.suppressed:
            return ReviewResult(
                suppressed=[query.raw],
                explanation="Query suppressed via -- prism: ignore comment.",
            )

        # 1. Run static rules
        static_issues = rules.run_all_rules(query.raw)

        # 2. Call LLM for optimisation suggestions
        try:
            result = self._llm_review(query.raw, schema_context, static_issues)
        except Exception as exc:
            logger.warning("[LLM Review] Failed, using static-only results: %s", exc)
            result = _build_static_only_result(static_issues)

        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _llm_review(
        self,
        query: str,
        schema_context: str,
        static_issues: list[Issue],
    ) -> ReviewResult:
        static_dicts = [i.model_dump() for i in static_issues]
        user_prompt = prompts.build_user_prompt(
            query, schema_context, static_dicts, None
        )

        raw = self._llm.complete_json(
            system=prompts.SYSTEM_PROMPT,
            user=user_prompt,
        )

        return _parse_llm_response(raw, static_issues)


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------




def _parse_llm_response(data: dict, static_issues: list[Issue]) -> ReviewResult:
    """Merge LLM response dict with static findings into a ReviewResult."""
    llm_issues = []
    for item in data.get("issues", []):
        try:
            llm_issues.append(Issue(**item))
        except Exception:
            pass  # Skip malformed issue objects

    # Deduplicate: drop LLM issues whose type is already in static findings
    static_types = {i.type for i in static_issues}
    merged_issues = list(static_issues) + [
        i for i in llm_issues if i.type not in static_types
    ]

    cost_raw = data.get("cost_analysis", {})
    cost = CostAnalysis(
        level=cost_raw.get("level", "low"),
        basis=cost_raw.get("basis", "static"),
        reason=cost_raw.get("reason", ""),
        estimated_improvement=cost_raw.get("estimated_improvement", ""),
    )

    return ReviewResult(
        issues=merged_issues,
        optimized_query=data.get("optimized_query", ""),
        index_suggestions=data.get("index_suggestions", []),
        migration_warnings=data.get("migration_warnings", []),
        cost_analysis=cost,
        explanation=data.get("explanation", ""),
        suppressed=data.get("suppressed", []),
    )


def _build_static_only_result(static_issues: list[Issue]) -> ReviewResult:
    level = "low"
    if any(i.severity == "high" for i in static_issues):
        level = "high"
    elif any(i.severity == "medium" for i in static_issues):
        level = "medium"

    return ReviewResult(
        issues=static_issues,
        cost_analysis=CostAnalysis(
            level=level,
            basis="static",
            reason="LLM analysis unavailable; result based on static rules only.",
        ),
        explanation="Static analysis completed. LLM optimisation suggestions unavailable.",
    )
