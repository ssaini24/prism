"""GitHub webhook receiver with HMAC-SHA256 signature validation."""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import Header, HTTPException, Request

from config import settings

logger = logging.getLogger(__name__)


async def verify_signature(request: Request, x_hub_signature_256: str | None = Header(None)) -> bytes:
    """
    FastAPI dependency that validates the GitHub webhook HMAC signature.

    GitHub signs the raw request body with the webhook secret using
    HMAC-SHA256 and sends it in the X-Hub-Signature-256 header.

    Returns the raw body bytes on success; raises HTTP 401/403 on failure.
    """
    if not settings.github_webhook_secret:
        logger.warning("GITHUB_WEBHOOK_SECRET is not set — skipping signature validation.")
        return await request.body()

    if x_hub_signature_256 is None:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header.")

    body = await request.body()
    expected = _compute_signature(body, settings.github_webhook_secret)

    if not hmac.compare_digest(expected, x_hub_signature_256):
        logger.warning("Webhook signature mismatch — possible spoofed request.")
        raise HTTPException(status_code=403, detail="Invalid webhook signature.")

    return body


def _compute_signature(body: bytes, secret: str) -> str:
    mac = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def extract_pr_info(payload: dict) -> tuple[str, str, int] | None:
    """
    Extract (owner, repo, pr_number) from a GitHub pull_request webhook payload.

    Returns None if the event is not a PR open/synchronize event.
    """
    action = payload.get("action")
    if action not in {"opened", "synchronize", "reopened"}:
        return None

    pr = payload.get("pull_request", {})
    pr_number = pr.get("number")
    repo = payload.get("repository", {})
    full_name = repo.get("full_name", "")

    if not full_name or not pr_number:
        return None

    parts = full_name.split("/", 1)
    if len(parts) != 2:
        return None

    owner, repo_name = parts
    return owner, repo_name, pr_number
