"""Translates ORM code to raw SQL via LLM."""
from __future__ import annotations

import logging

from core.llm_client import LLMClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPTS: dict[str, str] = {
    "eloquent": """\
You are a Laravel Eloquent and Query Builder expert.
Translate the given PHP code to equivalent raw SQL queries.

Rules:
- Return ONLY the raw SQL queries, one per line.
- No explanation, no markdown, no code fences.
- If a line is a Schema Builder migration, translate it to the equivalent DDL (CREATE TABLE, ALTER TABLE, DROP TABLE, etc.).
- If a line cannot be translated to SQL (e.g. pure PHP logic), skip it.
- If there are no translatable queries, return the single word: NONE
""",
}


def translate(orm: str, code: str, llm: LLMClient) -> list[str]:
    """
    Translate ORM code block to a list of raw SQL strings.

    Returns empty list if translation fails or yields nothing.
    """
    system = _SYSTEM_PROMPTS.get(orm)
    if not system:
        logger.warning("No translation prompt for ORM: %s", orm)
        return []

    try:
        raw = llm.complete(system=system, user=code)
        if not raw or raw.strip().upper() == "NONE":
            return []
        queries = [line.strip() for line in raw.splitlines() if line.strip()]
        logger.info("ORM translator (%s) produced %d SQL query/queries.", orm, len(queries))
        return queries
    except Exception as exc:
        logger.warning("ORM translation failed for %s: %s", orm, exc)
        return []
