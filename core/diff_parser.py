"""Extracts SQL queries from GitHub PR git diffs."""
from __future__ import annotations

import re

from models.review import ExtractedQuery

# Patterns that suggest a line contains SQL
_SQL_KEYWORDS = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|REPLACE|MERGE|WITH)\b",
    re.IGNORECASE,
)

# Suppression marker
_SUPPRESS_MARKER = re.compile(r"--\s*prism:\s*ignore", re.IGNORECASE)

# Heuristic: a SQL string is likely embedded in code as a quoted string or raw assignment
_QUERY_EXTRACTION = re.compile(
    r"""(?:["'`]|r["'])((?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|REPLACE|MERGE|WITH)[\s\S]+?)(?:["'`])""",
    re.IGNORECASE,
)


def parse_diff(diff_text: str) -> list[ExtractedQuery]:
    """
    Parse a unified diff and extract SQL queries from added lines (+).

    Returns a list of ExtractedQuery objects with file, line number, and
    suppression flag.
    """
    queries: list[ExtractedQuery] = []
    current_file = "unknown"
    current_line = 0
    pending_lines: list[tuple[str, int, str]] = []  # (raw_line, line_no, file)

    for raw_line in diff_text.splitlines():
        # Track current file from diff headers
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:].strip()
            current_line = 0
            continue
        if raw_line.startswith("@@"):
            # Extract new-file line number from hunk header: @@ -a,b +c,d @@
            match = re.search(r"\+(\d+)", raw_line)
            if match:
                current_line = int(match.group(1)) - 1
            continue

        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            current_line += 1
            line_content = raw_line[1:]  # strip leading +
            if _SQL_KEYWORDS.search(line_content):
                pending_lines.append((line_content, current_line, current_file))
        elif not raw_line.startswith("-"):
            # Context line — still advances new-file line counter
            current_line += 1

    # Consolidate consecutive SQL lines into single query blocks
    queries = _consolidate(pending_lines)
    return queries


def _consolidate(
    lines: list[tuple[str, int, str]],
) -> list[ExtractedQuery]:
    """Merge consecutive lines from the same file into query blocks."""
    if not lines:
        return []

    results: list[ExtractedQuery] = []
    block_lines: list[str] = [lines[0][0]]
    block_start = lines[0][1]
    block_file = lines[0][2]

    for content, lineno, file in lines[1:]:
        if file == block_file and lineno == block_start + len(block_lines):
            block_lines.append(content)
        else:
            results.append(_make_query(block_lines, block_start, block_file))
            block_lines = [content]
            block_start = lineno
            block_file = file

    results.append(_make_query(block_lines, block_start, block_file))
    return results


def _make_query(lines: list[str], start_line: int, file: str) -> ExtractedQuery:
    raw = "\n".join(lines).strip()
    suppressed = bool(_SUPPRESS_MARKER.search(raw))
    # Strip suppression comment from the query itself
    clean = re.sub(r"--\s*prism:\s*ignore.*$", "", raw, flags=re.IGNORECASE | re.MULTILINE).strip()
    return ExtractedQuery(raw=clean, file=file, line=start_line, suppressed=suppressed)
