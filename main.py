"""Prism — AI-powered PR reviewer. FastAPI entry point."""
from __future__ import annotations

import logging
import sys

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from config import settings
from core.analyser import Analyser
from gh.commenter import PRCommenter
from gh.webhook import extract_pr_info, extract_review_comment_feedback, verify_signature

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Prism",
    description="AI-powered PR reviewer — DB query optimisation module.",
    version="1.0.0",
)

from core.feedback_store import init_db
init_db()

_analyser = Analyser()
_commenter = PRCommenter()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    """Liveness probe used by Docker and load balancers."""
    return {"status": "ok"}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    body: bytes = Depends(verify_signature),
    x_github_event: str | None = Header(None),
) -> JSONResponse:
    """
    Receives GitHub webhook events.

    Only pull_request events with action opened/synchronize/reopened are
    processed. All others return 200 immediately.
    """
    import json

    if x_github_event == "pull_request_review_comment":
        return await _handle_review_comment(body)

    if x_github_event != "pull_request":
        return JSONResponse({"message": "Event ignored."}, status_code=200)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    pr_info = extract_pr_info(payload)
    if pr_info is None:
        return JSONResponse({"message": "PR action ignored."}, status_code=200)

    owner, repo_name, pr_number = pr_info
    commit_sha = payload.get("pull_request", {}).get("head", {}).get("sha", "")

    from core.llm_client import log_usage_summary, reset_usage_tracker
    reset_usage_tracker()
    logger.info("Processing PR #%d for %s/%s", pr_number, owner, repo_name)

    # Fetch the diff from GitHub
    diff_text = _fetch_pr_diff(owner, repo_name, pr_number)
    if not diff_text:
        return JSONResponse({"message": "Could not fetch PR diff."}, status_code=200)

    # Run analysis
    results = _analyser.analyse_pr(diff_text, repo=f"{owner}/{repo_name}")

    # Post review comments
    _commenter.post_review(owner, repo_name, pr_number, results, commit_sha)

    # PR-level summary log
    all_issues = [i for _, r in results for i in r.issues]
    high   = sum(1 for i in all_issues if i.severity == "high")
    medium = sum(1 for i in all_issues if i.severity == "medium")
    low    = sum(1 for i in all_issues if i.severity == "low")
    files  = sorted({q.file for q, r in results if r.issues})

    log_usage_summary()
    logger.info("═" * 60)
    logger.info("  PR #%d REVIEW COMPLETE — %s/%s", pr_number, owner, repo_name)
    logger.info("  Issues : %d total (🔴 %d high · 🟠 %d medium · 🟡 %d low)",
                len(all_issues), high, medium, low)
    logger.info("  Files  : %d reviewed", len(files))
    for f in files:
        count = sum(len(r.issues) for q, r in results if q.file == f)
        logger.info("           • %s (%d issue(s))", f, count)
    logger.info("═" * 60)

    return JSONResponse(
        {"message": "Review posted.", "issues_found": len(all_issues)},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



async def _handle_review_comment(body: bytes) -> JSONResponse:
    """
    Process engineer replies to Prism inline comments.

    Detects positive/negative signals and stores them in the feedback DB.
    """
    import json, re
    from core.feedback_store import detect_signal, record_feedback
    from github import Github

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"message": "Invalid JSON."}, status_code=400)

    info = extract_review_comment_feedback(payload)
    if not info:
        return JSONResponse({"message": "Not a tracked reply."}, status_code=200)

    signal = detect_signal(info["reply_body"])
    if signal is None:
        logger.debug("Reply on %s contained no recognisable feedback signal.", info["repo"])
        return JSONResponse({"message": "No signal detected."}, status_code=200)

    # Fetch the parent comment to extract the Prism rule type
    _ISSUE_TYPE_RE = re.compile(r"\*\*\[(\w+)\]\*\*")
    try:
        import requests as _requests
        resp = _requests.get(
            f"https://api.github.com/repos/{info['repo']}/pulls/comments/{info['in_reply_to_id']}",
            headers={
                "Authorization": f"token {settings.github_token}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Could not fetch parent comment %d: HTTP %d", info["in_reply_to_id"], resp.status_code)
            return JSONResponse({"message": "Parent comment not found."}, status_code=200)
        parent_body = resp.json().get("body", "")
        match = _ISSUE_TYPE_RE.search(parent_body)
        if not match:
            logger.debug("Parent comment is not a Prism comment — skipping feedback.")
            return JSONResponse({"message": "Parent is not a Prism comment."}, status_code=200)
        rule = match.group(1)
    except Exception as exc:
        logger.warning("Could not fetch parent comment %d: %s", info["in_reply_to_id"], exc)
        return JSONResponse({"message": "Could not resolve parent comment."}, status_code=200)

    record_feedback(
        rule=rule,
        repo=info["repo"],
        file_path=info["file_path"],
        signal=signal,
        comment=info["reply_body"],
    )

    direction = "positive" if signal > 0 else "negative"
    logger.info(
        "Feedback loop: %s signal for rule [%s] in %s — %s",
        direction, rule, info["repo"], info["file_path"],
    )

    # Reply to engineer's comment acknowledging the feedback
    _post_feedback_acknowledgement(
        repo=info["repo"],
        in_reply_to_id=info["in_reply_to_id"],
        rule=rule,
        file_path=info["file_path"],
        signal=signal,
    )

    return JSONResponse({"message": f"Feedback recorded: {direction} for {rule}."}, status_code=200)


def _post_feedback_acknowledgement(
    repo: str,
    in_reply_to_id: int,
    rule: str,
    file_path: str,
    signal: int,
) -> None:
    """Reply to the engineer's comment confirming feedback was saved."""
    import requests as _requests
    from core.feedback_store import _path_context, get_adjustment

    ctx = _path_context(file_path) or "this repo"
    adj = get_adjustment(rule, repo, file_path)
    net = abs(adj["net_signal"])

    if signal < 0:
        if adj.get("skip"):
            body = (
                f"🧠 Understood — I've added this to my learnings. "
                f"`[{rule}]` has been flagged {net} time(s) in `{ctx}`, "
                f"so I'll **skip** it there going forward."
            )
        else:
            body = (
                f"🧠 Got it — I've saved this to my learnings. "
                f"I'll lower the confidence for `[{rule}]` in `{ctx}` on future reviews "
                f"({net} negative signal(s) recorded)."
            )
    else:
        body = (
            f"✅ Thanks for confirming! I've noted that `[{rule}]` is a real issue in `{ctx}`. "
            f"I'll keep flagging similar patterns here ({net} positive signal(s) recorded)."
        )

    # Find the PR number from a comment on this repo — we need it for the reply endpoint.
    # GitHub's reply endpoint requires pull_number, so we fetch it from the parent comment.
    try:
        parent_resp = _requests.get(
            f"https://api.github.com/repos/{repo}/pulls/comments/{in_reply_to_id}",
            headers={
                "Authorization": f"token {settings.github_token}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=10,
        )
        if parent_resp.status_code != 200:
            logger.warning("Could not fetch parent comment to reply: HTTP %d", parent_resp.status_code)
            return
        pull_number = parent_resp.json().get("pull_request_review_id")
        # pull_request_url gives us the PR number directly
        pr_url = parent_resp.json().get("pull_request_url", "")
        pr_number = int(pr_url.rstrip("/").split("/")[-1]) if pr_url else None
        if not pr_number:
            logger.warning("Could not determine PR number for feedback reply.")
            return

        resp = _requests.post(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments",
            headers={
                "Authorization": f"token {settings.github_token}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"body": body, "in_reply_to": in_reply_to_id},
            timeout=10,
        )
        if resp.status_code in (200, 201):
            logger.info("Feedback acknowledgement posted for rule [%s].", rule)
        else:
            logger.warning("Failed to post feedback acknowledgement: HTTP %d — %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("Could not post feedback acknowledgement: %s", exc)


def _fetch_pr_diff(owner: str, repo_name: str, pr_number: int) -> str:
    """Fetch the unified diff for a PR via the GitHub REST API."""
    try:
        import requests
        response = requests.get(
            f"https://api.github.com/repos/{owner}/{repo_name}/pulls/{pr_number}",
            headers={
                "Authorization": f"token {settings.github_token}",
                "Accept": "application/vnd.github.v3.diff",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.text
    except Exception as exc:
        logger.error("Failed to fetch PR diff: %s", exc)
        return ""
