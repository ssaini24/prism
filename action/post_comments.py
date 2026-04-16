"""Merges ORM and static analysis artifact JSON files and posts PR review comments."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

# Make the project root importable when this script is invoked from the action/ subdirectory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gh.commenter import PRCommenter  # noqa: E402
from models.review import ExtractedQuery, ReviewResult  # noqa: E402

logger = logging.getLogger(__name__)


def load_artifact(path: str) -> list[tuple[ExtractedQuery, ReviewResult]]:
    """Load a results JSON artifact file.

    Returns a list of (ExtractedQuery, ReviewResult) pairs.
    Returns an empty list if the file is missing or malformed.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data: list[dict[str, Any]] = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        logger.debug("Could not load artifact %s: %s", path, exc)
        return []

    pairs: list[tuple[ExtractedQuery, ReviewResult]] = []
    for entry in data:
        try:
            query = ExtractedQuery(**entry["query"])
            result = ReviewResult(**entry["result"])
            pairs.append((query, result))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed artifact entry: %s", exc)

    return pairs


def post(
    orm_results: str,
    static_results: str,
    owner: str,
    repo: str,
    pr_number: int,
    sha: str,
) -> None:
    """Load both artifact files, merge them, and post review comments to the PR."""
    orm_pairs = load_artifact(orm_results)
    static_pairs = load_artifact(static_results)
    merged = orm_pairs + static_pairs

    PRCommenter().post_review(
        owner=owner,
        repo_name=repo,
        pr_number=pr_number,
        results=merged,
        commit_sha=sha,
    )


def main() -> None:
    """CLI entry point for posting PR review comments from artifact files."""
    parser = argparse.ArgumentParser(
        description="Post Prism review comments to a GitHub PR from artifact JSON files."
    )
    parser.add_argument("--orm-results", required=True, help="Path to ORM analysis results JSON")
    parser.add_argument("--static-results", required=True, help="Path to static analysis results JSON")
    parser.add_argument("--owner", required=True, help="GitHub repository owner")
    parser.add_argument("--repo", required=True, help="GitHub repository name")
    parser.add_argument("--pr", required=True, type=int, help="Pull request number")
    parser.add_argument("--sha", required=True, help="Commit SHA to attach comments to")
    args = parser.parse_args()

    post(
        orm_results=args.orm_results,
        static_results=args.static_results,
        owner=args.owner,
        repo=args.repo,
        pr_number=args.pr,
        sha=args.sha,
    )


if __name__ == "__main__":
    main()
