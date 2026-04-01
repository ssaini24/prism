"""Main orchestrator — routes PR diff to the correct reviewer(s)."""
from __future__ import annotations

import logging

from core.diff_parser import parse_code_blocks, parse_diff
from core.llm_client import create_llm_client
from models.review import ExtractedQuery, ReviewResult
from reviewers.base_reviewer import BaseReviewer
from reviewers.code_review.reviewer import CodeReviewAgent
from reviewers.db_query.reviewer import DBQueryReviewer
from reviewers.orm.reviewer import ORMReviewer

logger = logging.getLogger(__name__)


class Analyser:
    """
    Coordinates the full review pipeline for a PR.

    Two parsers run against the diff:
      - parse_diff()        → SQL blocks  → DBQueryReviewer
      - parse_code_blocks() → Code blocks → CodeReviewAgent

    Each reviewer's can_review() gates which blocks it processes.
    """

    def __init__(self, reviewers: list[BaseReviewer] | None = None) -> None:
        from config import settings
        llm = create_llm_client()
        self._sql_reviewers: list[BaseReviewer] = [DBQueryReviewer(llm_client=llm)]
        self._code_reviewers: list[BaseReviewer] = (
            [CodeReviewAgent(llm_client=llm)] if settings.enable_code_review else []
        )
        self._orm_reviewers: list[BaseReviewer] = (
            [ORMReviewer(llm_client=llm)] if settings.enable_orm_review else []
        )
        if reviewers is not None:
            # Allow full override in tests
            self._sql_reviewers = reviewers
            self._code_reviewers = []
            self._orm_reviewers = []

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
            List of (query, result) pairs — one entry per block reviewed.
        """
        sql_blocks = parse_diff(diff_text)
        code_blocks = parse_code_blocks(diff_text)
        logger.info(
            "Extracted %d SQL block(s) and %d code block(s) from diff.",
            len(sql_blocks),
            len(code_blocks),
        )

        results: list[tuple[ExtractedQuery, ReviewResult]] = []
        results.extend(self._run(sql_blocks, self._sql_reviewers, schema_context))
        results.extend(self._run(code_blocks, self._code_reviewers, schema_context))
        results.extend(self._run(code_blocks, self._orm_reviewers, schema_context))
        return results

    def _run(
        self,
        blocks: list[ExtractedQuery],
        reviewers: list[BaseReviewer],
        schema_context: str,
    ) -> list[tuple[ExtractedQuery, ReviewResult]]:
        results: list[tuple[ExtractedQuery, ReviewResult]] = []
        for block in blocks:
            for reviewer in reviewers:
                if not reviewer.can_review(block):
                    logger.debug(
                        "Reviewer %s skipped block at %s:%d",
                        reviewer.name, block.file, block.line,
                    )
                    continue
                try:
                    result = reviewer.review(block, schema_context=schema_context)
                    results.append((block, result))
                    logger.info(
                        "Reviewer %s completed for %s:%d — %d issue(s).",
                        reviewer.name, block.file, block.line, len(result.issues),
                    )
                except Exception:
                    logger.exception(
                        "Reviewer %s raised an exception for block at %s:%d.",
                        reviewer.name, block.file, block.line,
                    )
        return results
