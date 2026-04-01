"""Detects ORM framework from file extension and code patterns."""
from __future__ import annotations

import re

# Eloquent patterns — covers models, query builder, schema builder, facades
_ELOQUENT_PATTERNS = re.compile(
    r"""
    \$\w+\s*->\s*(where|find|first|get|all|create|update|delete|save|destroy|insert|select|join|orderBy|groupBy|having|limit|offset|with|whereIn|whereNull|whereNotNull|orWhere)\s*\(
    | DB\s*::\s*(table|select|insert|update|delete|statement|raw|transaction)
    | Schema\s*::\s*(create|drop|table|hasTable|rename|dropIfExists)
    | \w+\s*::\s*(where|find|first|all|create|destroy|with|select)
    | Blueprint\s*\$
    | ->\s*belongsTo\s*\(
    | ->\s*hasMany\s*\(
    | ->\s*hasOne\s*\(
    """,
    re.VERBOSE | re.IGNORECASE,
)


def detect(file_path: str, code: str) -> str | None:
    """
    Detect ORM framework from file path and code content.

    Returns ORM name string or None if not ORM code.
    Currently supports: 'eloquent'
    """
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""

    if ext == "php" and _ELOQUENT_PATTERNS.search(code):
        return "eloquent"

    return None
