"""Posts structured review comments back to a GitHub PR."""
from __future__ import annotations

import logging

from github import Github, GithubException
from github.PullRequest import PullRequest

from config import settings
from models.review import ExtractedQuery, Issue, ReviewResult

logger = logging.getLogger(__name__)

_SEVERITY_EMOJI = {"low": "🟡", "medium": "🟠", "high": "🔴"}
_CONFIDENCE_LABEL = {"low": "low confidence", "medium": "medium confidence", "high": "high confidence"}


class PRCommenter:
    """Posts inline and summary review comments to a GitHub pull request."""

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
        """
        Post a consolidated review with inline comments and a summary body.

        Args:
            owner: Repository owner (user or org).
            repo_name: Repository name.
            pr_number: Pull request number.
            results: List of (query, result) pairs from the analyser.
            commit_sha: The HEAD commit SHA of the PR (required for inline comments).
        """
        repo = self._gh.get_repo(f"{owner}/{repo_name}")
        pr: PullRequest = repo.get_pull(pr_number)

        total_issues = sum(len(r.issues) for _, r in results)
        if total_issues == 0:
            logger.info("No issues found — posting clean bill of health.")
            pr.create_issue_comment(_clean_comment())
            return

        # Build inline comments per file/line where possible
        review_comments = []
        for query, result in results:
            if result.suppressed and not result.issues:
                continue
            for issue in result.issues:
                body = _format_inline_issue(issue, result)
                if query.line > 0:
                    try:
                        review_comments.append(
                            {
                                "path": query.file,
                                "line": query.line,
                                "body": body,
                            }
                        )
                    except Exception:
                        pass  # Fall through to summary

        summary_body = _build_summary(results)

        try:
            if review_comments:
                pr.create_review(
                    commit=repo.get_commit(commit_sha),
                    body=summary_body,
                    event="COMMENT",
                    comments=review_comments,
                )
            else:
                pr.create_issue_comment(summary_body)
        except GithubException as exc:
            logger.error("Failed to post review: %s", exc)
            # Fallback: post as a plain issue comment
            try:
                pr.create_issue_comment(summary_body)
            except GithubException:
                logger.exception("Fallback comment also failed.")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


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
    return "\n".join(lines)


def _build_summary(results: list[tuple[ExtractedQuery, ReviewResult]]) -> str:
    all_issues = [(q, i) for q, r in results for i in r.issues]
    high = [i for _, i in all_issues if i.severity == "high"]
    medium = [i for _, i in all_issues if i.severity == "medium"]
    low = [i for _, i in all_issues if i.severity == "low"]

    lines = [
        "## 🔍 Prism DB Query Review",
        "",
        f"Found **{len(all_issues)} issue(s)**: "
        f"🔴 {len(high)} high · 🟠 {len(medium)} medium · 🟡 {len(low)} low",
        "",
    ]

    for query, result in results:
        if not result.issues:
            continue
        lines.append(f"### `{query.file}` (line {query.line})")
        lines.append(f"```sql\n{query.raw[:500]}\n```")
        for issue in result.issues:
            emoji = _SEVERITY_EMOJI.get(issue.severity, "⚪")
            lines.append(f"- {emoji} **{issue.type}**: {issue.description}")
            lines.append(f"  > {issue.suggestion}")
        if result.index_suggestions:
            lines.append("")
            lines.append("**Index suggestions:**")
            for s in result.index_suggestions:
                lines.append(f"```sql\n{s}\n```")
        if result.migration_warnings:
            lines.append("")
            lines.append("**Migration warnings:**")
            for w in result.migration_warnings:
                lines.append(f"- ⚠️ {w}")
        if result.explanation:
            lines.append("")
            lines.append(f"_{result.explanation}_")
        lines.append("")

    lines += [
        "---",
        "_Review generated by [Prism](https://github.com/your-org/prism). "
        "Suppress a query with `-- prism: ignore`._",
    ]
    return "\n".join(lines)


def _clean_comment() -> str:
    return (
        "## 🔍 Prism DB Query Review\n\n"
        "✅ No SQL issues detected in this PR.\n\n"
        "---\n"
        "_Review generated by Prism. Suppress a query with `-- prism: ignore`._"
    )
