"""Prism — AI-powered PR reviewer. FastAPI entry point."""
from __future__ import annotations

import logging
import sys

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from config import settings
from core.analyser import Analyser
from gh.commenter import PRCommenter
from gh.webhook import extract_pr_info, verify_signature

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

    logger.info("Processing PR #%d for %s/%s", pr_number, owner, repo_name)

    # Fetch the diff from GitHub
    diff_text = _fetch_pr_diff(owner, repo_name, pr_number)
    if not diff_text:
        return JSONResponse({"message": "Could not fetch PR diff."}, status_code=200)

    # Run analysis
    results = _analyser.analyse_pr(diff_text)

    # Post review comments
    _commenter.post_review(owner, repo_name, pr_number, results, commit_sha)

    # PR-level summary log
    all_issues = [i for _, r in results for i in r.issues]
    high   = sum(1 for i in all_issues if i.severity == "high")
    medium = sum(1 for i in all_issues if i.severity == "medium")
    low    = sum(1 for i in all_issues if i.severity == "low")
    files  = sorted({q.file for q, r in results if r.issues})

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
