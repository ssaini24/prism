"""Main orchestrator — routes PR diff to DBQueryReviewer."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.diff_parser import parse_diff
from core.llm_client import create_llm_client
from models.review import ExtractedQuery, ReviewResult
from reviewers.base_reviewer import BaseReviewer
from reviewers.db_query.reviewer import DBQueryReviewer

logger = logging.getLogger(__name__)


class Analyser:
    """Runs static SQL analysis over a PR diff using DBQueryReviewer."""

    def __init__(self, reviewers: list[BaseReviewer] | None = None) -> None:
        llm = create_llm_client()
        self._sql_reviewers: list[BaseReviewer] = [DBQueryReviewer(llm_client=llm)]
        if reviewers is not None:
            # Allow full override in tests
            self._sql_reviewers = reviewers

    def analyse_pr(
        self,
        diff_text: str,
        schema_context: str = "",
        repo: str = "",
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

        results: list[tuple[ExtractedQuery, ReviewResult]] = []
        try:
            results.extend(self._run(sql_blocks, self._sql_reviewers, schema_context))
        except Exception:
            logger.exception("SQL review pipeline raised an exception.")

        return results

    def _run(
        self,
        blocks: list[ExtractedQuery],
        reviewers: list[BaseReviewer],
        schema_context: str,
    ) -> list[tuple[ExtractedQuery, ReviewResult]]:
        def _review_one(block: ExtractedQuery, reviewer: BaseReviewer):
            if not reviewer.can_review(block):
                logger.debug(
                    "Reviewer %s skipped block at %s:%d",
                    reviewer.name, block.file, block.line,
                )
                return None
            result = reviewer.review(block, schema_context=schema_context)
            return (block, result)

        work = [
            (block, reviewer)
            for block in blocks
            for reviewer in reviewers
        ]

        if not work:
            return []

        # Parallelize LLM calls across all (block, reviewer) pairs
        results: list[tuple[ExtractedQuery, ReviewResult]] = []
        with ThreadPoolExecutor(max_workers=max(1, min(len(work), 8))) as executor:
            futures = {executor.submit(_review_one, block, reviewer): (block, reviewer) for block, reviewer in work}
            for future in as_completed(futures):
                block, reviewer = futures[future]
                try:
                    out = future.result()
                    if out is not None:
                        results.append(out)
                except Exception:
                    logger.exception("Review failed for %s:%d", block.file, block.line)

        return results
