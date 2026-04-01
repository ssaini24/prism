"""Posts structured review comments back to a GitHub PR."""
from __future__ import annotations

import logging
import re

from github import Github, GithubException
from github.PullRequest import PullRequest

from config import settings
from models.review import ExtractedQuery, Issue, ReviewResult

logger = logging.getLogger(__name__)

_SEVERITY_EMOJI = {"low": "🟡", "medium": "🟠", "high": "🔴"}
_CONFIDENCE_LABEL = {"low": "low confidence", "medium": "medium confidence", "high": "high confidence"}

# Matches the issue type embedded in Prism comment bodies: **[issue_type]**
_ISSUE_TYPE_RE = re.compile(r"\*\*\[(\w+)\]\*\*")


class PRCommenter:
    """Posts inline review comments to a GitHub pull request."""

    def __init__(self) -> None:
        self._gh = Github(settings.github_token)

    def post_review(
        self,
        owner: str,
        repo_name: str,
        pr_number: int,
        results: list[tuple[ExtractedQuery, ReviewResult]],
        commit_sha: str,
    ) -> None:
        repo = self._gh.get_repo(f"{owner}/{repo_name}")
        pr: PullRequest = repo.get_pull(pr_number)
        commit = repo.get_commit(commit_sha)

        total_issues = sum(len(r.issues) for _, r in results)
        if total_issues == 0:
            logger.info("No issues found — posting clean bill of health.")
            pr.create_issue_comment(_clean_comment())
            return

        # Fetch existing Prism inline comments keyed by (path, position, issue_type).
        # PyGithub does not expose `line` on fetched comments — only `position` is reliable.
        existing = _fetch_prism_comments(pr)
        existing_keys: dict[tuple[str, int, str], object] = {}  # key → comment object
        for c in existing:
            k = _comment_key(c)
            if k:
                existing_keys[k] = c
        logger.info("Found %d existing Prism comment(s) on PR #%d.", len(existing_keys), pr_number)

        # Post new comments and track by (path, position, issue_type) using the
        # position returned by GitHub after each successful create_review_comment call.
        inline_count = 0
        skipped = 0
        resolved_keys: set[tuple[str, int, str]] = set()

        for query, result in results:
            if not result.issues or query.line <= 0:
                continue
            for issue in result.issues:
                body = _format_inline_issue(issue, result)
                try:
                    posted = pr.create_review_comment(
                        body=body,
                        commit=commit,
                        path=query.file,
                        line=query.line,
                        side="RIGHT",
                    )
                    pos = getattr(posted, "position", None)
                    if pos is not None:
                        new_key = (query.file, pos, issue.type)
                        if new_key in existing_keys:
                            # Duplicate — delete the one we just posted, keep the existing.
                            # Mark the existing key as still active so it isn't auto-resolved.
                            try:
                                posted.delete()
                                skipped += 1
                                logger.debug(
                                    "Skipped duplicate comment: %s pos=%d [%s]",
                                    query.file, pos, issue.type,
                                )
                            except GithubException:
                                pass
                            resolved_keys.add(new_key)  # preserve the existing comment
                        else:
                            inline_count += 1
                            resolved_keys.add(new_key)
                            logger.info(
                                "Inline comment posted: %s:%d pos=%d [%s]",
                                query.file, query.line, pos, issue.type,
                            )
                    else:
                        inline_count += 1
                        logger.info(
                            "Inline comment posted: %s:%d [%s]",
                            query.file, query.line, issue.type,
                        )
                except GithubException as exc:
                    logger.warning(
                        "Inline comment failed for %s:%d [%s] (status=%s): %s",
                        query.file, query.line, issue.type, exc.status, exc.data,
                    )

        # Delete existing comments whose issue was not re-raised (addressed in new commit)
        resolved = 0
        for key, comment in existing_keys.items():
            if key not in resolved_keys:
                try:
                    comment.delete()
                    resolved += 1
                    logger.info("Auto-resolved outdated comment: %s pos=%d [%s]", *key)
                except GithubException as exc:
                    logger.warning("Could not delete outdated comment %d: %s", comment.id, exc)

        logger.info(
            "Comments — posted: %d | skipped (duplicate): %d | auto-resolved: %d",
            inline_count, skipped, resolved,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_prism_comments(pr: PullRequest) -> list:
    """Return all existing inline review comments posted by Prism."""
    try:
        return [
            c for c in pr.get_review_comments()
            if _ISSUE_TYPE_RE.search(c.body)
        ]
    except GithubException as exc:
        logger.warning("Could not fetch existing comments: %s", exc)
        return []


def _comment_key(comment) -> tuple[str, int, str] | None:
    """Extract (path, position, issue_type) from a Prism comment. Uses diff position — the only
    line identifier reliably exposed by PyGithub on fetched review comments."""
    match = _ISSUE_TYPE_RE.search(comment.body)
    if not match:
        return None
    pos = getattr(comment, "position", None)
    if not pos:
        return None
    return (comment.path, pos, match.group(1))


def _format_inline_issue(issue: Issue, result: ReviewResult) -> str:
    emoji = _SEVERITY_EMOJI.get(issue.severity, "⚪")
    confidence = _CONFIDENCE_LABEL.get(issue.confidence, issue.confidence)
    lines = [
        f"{emoji} **[{issue.type}]** _{confidence}_",
        "",
        issue.description,
        "",
        f"**Suggestion:** {issue.suggestion}",
    ]
    if result.optimized_query:
        lines += [
            "",
            "**Optimized query:**",
            f"```sql\n{result.optimized_query}\n```",
        ]
    if result.index_suggestions:
        lines += ["", "**Index suggestions:**"]
        for s in result.index_suggestions:
            lines.append(f"```sql\n{s}\n```")
    return "\n".join(lines)


def _clean_comment() -> str:
    return (
        "## 🔍 Prism Code Review\n\n"
        "✅ No issues detected in this PR.\n\n"
        "---\n"
        "_Review generated by [Prism](https://github.com/ssaini24/prism). "
        "Suppress a block with `-- prism: ignore`._"
    )
