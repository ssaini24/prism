"""ORM reviewer — detects ORM code, translates to SQL, runs existing static rules."""
from __future__ import annotations

import logging

from core.llm_client import LLMClient, create_llm_client
from core.orm_detector import detect
from core.orm_translator import translate
from models.review import ExtractedQuery, Issue, ReviewResult
from reviewers.base_reviewer import BaseReviewer
from reviewers.db_query import rules
from reviewers.db_query.reviewer import _build_static_only_result, _parse_llm_response
from reviewers.db_query import prompts as sql_prompts

logger = logging.getLogger(__name__)


class ORMReviewer(BaseReviewer):
    """
    Reviews ORM code by translating it to raw SQL first,
    then running the existing static rules and LLM reviewer.
    """

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self._llm = llm_client or create_llm_client()

    @property
    def name(self) -> str:
        return "ORM Reviewer"

    def can_review(self, query: ExtractedQuery) -> bool:
        if query.suppressed:
            return False
        orm = detect(query.file, query.raw)
        return orm is not None

    def review(self, query: ExtractedQuery, schema_context: str = "") -> ReviewResult:
        orm = detect(query.file, query.raw)
        if not orm:
            return ReviewResult(explanation="No ORM patterns detected.")

        # Step 1: translate ORM → raw SQL
        sql_queries = translate(orm, query.raw, self._llm)
        if not sql_queries:
            logger.info("ORM translation yielded no SQL for %s:%d", query.file, query.line)
            return ReviewResult(explanation=f"Could not translate {orm} code to SQL.")

        # Step 2: run static rules + EXPLAIN + LLM on each translated query
        from config import settings
        from core.db_explainer import explain

        all_issues: list[Issue] = []
        for sql in sql_queries:
            static_issues = rules.run_all_rules(sql)
            all_issues.extend(static_issues)

            explain_result = None
            if settings.enable_db_explain:
                exp = explain(sql)
                if exp and exp.has_issues():
                    explain_result = exp.to_dict()
                    logger.info("ORM EXPLAIN: %s", exp.summary())

            try:
                user_prompt = sql_prompts.build_user_prompt(
                    sql, schema_context,
                    [i.model_dump() for i in static_issues],
                    explain_result,
                )
                raw_response = self._llm.complete_json(
                    system=sql_prompts.SYSTEM_PROMPT,
                    user=user_prompt,
                )
                result = _parse_llm_response(raw_response, static_issues)
                all_issues.extend([i for i in result.issues if i not in all_issues])
            except Exception as exc:
                logger.warning("LLM review failed for translated SQL from %s:%d — %s", query.file, query.line, exc)

        if not all_issues:
            return ReviewResult(explanation=f"No issues found in translated {orm} queries.")

        return _build_static_only_result(all_issues)
