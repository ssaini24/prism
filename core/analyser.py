"""Main orchestrator — routes PR diff to the correct reviewer(s)."""
from __future__ import annotations

import logging

from core.diff_parser import parse_diff
from core.llm_client import LLMClient
from models.review import ExtractedQuery, ReviewResult
from reviewers.base_reviewer import BaseReviewer
from reviewers.db_query.reviewer import DBQueryReviewer

logger = logging.getLogger(__name__)


class Analyser:
    """
    Coordinates the full review pipeline for a PR.

    1. Parses the diff to extract queries.
    2. Runs each applicable reviewer against each query.
    3. Returns a mapping of query → ReviewResult.
    """

    def __init__(self, reviewers: list[BaseReviewer] | None = None) -> None:
        llm = LLMClient()
        self._reviewers: list[BaseReviewer] = reviewers or [
            DBQueryReviewer(llm_client=llm),
        ]

    def analyse_pr(
        self,
        diff_text: str,
        schema_context: str = "",
    ) -> list[tuple[ExtractedQuery, ReviewResult]]:
        """
        Run all reviewers over the PR diff.

        Args:
            diff_text: Full unified diff string from the GitHub API.
            schema_context: Optional DDL/migration context as a plain string.

        Returns:
            List of (query, result) pairs — one entry per query reviewed.
            Suppressed queries are included with suppressed=True in the result.
        """
        queries = parse_diff(diff_text)
        logger.info("Extracted %d SQL query/block(s) from diff.", len(queries))

        results: list[tuple[ExtractedQuery, ReviewResult]] = []
        for query in queries:
            for reviewer in self._reviewers:
                if not reviewer.can_review(query):
                    logger.debug(
                        "Reviewer %s skipped query at %s:%d",
                        reviewer.name,
                        query.file,
                        query.line,
                    )
                    continue
                try:
                    result = reviewer.review(query, schema_context=schema_context)
                    results.append((query, result))
                    logger.info(
                        "Reviewer %s completed for %s:%d — %d issue(s).",
                        reviewer.name,
                        query.file,
                        query.line,
                        len(result.issues),
                    )
                except Exception:
                    logger.exception(
                        "Reviewer %s raised an exception for query at %s:%d.",
                        reviewer.name,
                        query.file,
                        query.line,
                    )

        return results
