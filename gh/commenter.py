"""Posts structured review comments back to a GitHub PR."""
from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from github import Github, GithubException
from github.PullRequest import PullRequest

from config import settings
from gh.auth import get_github_client, get_token
from models.review import ExtractedQuery, Issue, ReviewResult

logger = logging.getLogger(__name__)

_SEVERITY_EMOJI = {"low": "🟡", "medium": "🟠", "high": "🔴"}
_CONFIDENCE_LABEL = {"low": "low confidence", "medium": "medium confidence", "high": "high confidence"}

# Matches the issue type embedded in Prism comment bodies: **[issue_type]**
_ISSUE_TYPE_RE = re.compile(r"\*\*\[(\w+)\]\*\*")


class PRCommenter:
    """Posts inline review comments to a GitHub pull request."""

    def __init__(self) -> None:
        self._repo_full_name = ""

    def _gh(self, repo_full_name: str = "") -> Github:
        return get_github_client(repo_full_name or self._repo_full_name)

    def post_review(
        self,
        owner: str,
        repo_name: str,
        pr_number: int,
        results: list[tuple[ExtractedQuery, ReviewResult]],
        commit_sha: str,
    ) -> None:
        repo_full_name = f"{owner}/{repo_name}"
        gh = self._gh(repo_full_name)
        repo = gh.get_repo(repo_full_name)
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

        # Post new comments in parallel and track by (path, position, issue_type).
        inline_count = 0
        skipped = 0
        resolved_keys: set[tuple[str, int, str]] = set()
        lock = threading.Lock()

        def _post_one(query: ExtractedQuery, issue: Issue) -> None:
            nonlocal inline_count, skipped
            body = _format_inline_issue(issue, result_map[id(query)])
            try:
                posted = pr.create_review_comment(
                    body=body,
                    commit=commit,
                    path=query.file,
                    line=query.line,
                    side="RIGHT",
                )
                pos = getattr(posted, "position", None)
                with lock:
                    if pos is not None:
                        new_key = (query.file, pos, issue.type)
                        if new_key in existing_keys:
                            try:
                                posted.delete()
                                skipped += 1
                                logger.debug(
                                    "Skipped duplicate comment: %s pos=%d [%s]",
                                    query.file, pos, issue.type,
                                )
                            except GithubException:
                                pass
                            resolved_keys.add(new_key)
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

        # Build a map so the closure can look up the ReviewResult for each query
        result_map = {id(query): result for query, result in results}

        work = [
            (query, issue)
            for query, result in results
            if result.issues and query.line > 0
            for issue in result.issues
        ]
        with ThreadPoolExecutor(max_workers=max(1, min(len(work), 6))) as executor:
            futures = [executor.submit(_post_one, query, issue) for query, issue in work]
            for future in as_completed(futures):
                future.result()  # re-raise any unexpected exceptions

        # Delete existing comments whose issue was not re-raised (addressed in new commit)
        resolved = 0
        def _delete_one(key, comment):
            nonlocal resolved
            try:
                comment.delete()
                with lock:
                    resolved += 1
                logger.info("Auto-resolved outdated comment: %s pos=%d [%s]", *key)
            except GithubException as exc:
                logger.warning("Could not delete outdated comment %d: %s", comment.id, exc)

        stale = [(k, c) for k, c in existing_keys.items() if k not in resolved_keys]
        if stale:
            with ThreadPoolExecutor(max_workers=min(len(stale), 6)) as executor:
                for future in as_completed([executor.submit(_delete_one, k, c) for k, c in stale]):
                    future.result()

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
