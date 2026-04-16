"""
PHP/Eloquent ORM reviewer that analyzes PR diffs using `claude -p` with
Laravel Boost MCP as the schema tool. Follows the same subprocess pattern
as core/db_explainer.py.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import tempfile

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a Senior Laravel ORM Specialist reviewing PHP code from a pull request diff.

You have access to live database schema through Laravel Boost MCP tools.
Before flagging any issue involving a table name, column, or index — call the
relevant tool to verify against the actual migrated schema.

Available tools: database-schema, database-query, application-info.

Objectives:
1. Schema Validation: call database-schema to verify tables and columns exist.
   Flag missing tables/columns as severity: high.
2. N+1 Detection: identify loops accessing relationships without eager loading.
   Suggest the correct ->with() call.
3. Column Selection: flag select() or pluck() opportunities for large datasets.
4. Performance: suggest chunk(), lazy(), or cursor() over get() where appropriate.
5. Modern Standards: flag non-idiomatic Laravel patterns.

Rules:
- Never assume a table/column exists — call a tool to verify.
- Set confidence: low if schema could not be verified (tool unavailable).
- Set cost_analysis.basis to "explain" when backed by schema/index data from
  tools; "static" otherwise.

Output ONLY a valid JSON array — no markdown, no prose:
[
  {
    "file": "<filename>",
    "line": <int>,
    "issues": [{"type": "...", "severity": "low|medium|high", "confidence": "low|medium|high", "line": <int>, "description": "...", "suggestion": "..."}],
    "optimized_query": "<string or empty>",
    "index_suggestions": ["<CREATE INDEX ...>"],
    "migration_warnings": [],
    "cost_analysis": {"level": "low|medium|high", "basis": "static|explain", "reason": "...", "estimated_improvement": ""},
    "explanation": "<2-3 sentences>"
  }
]

Valid issue types: select_star, missing_where_clause, function_on_indexed_column,
join_without_condition, n_plus_one_pattern, destructive_ddl, unsafe_alter_table,
full_table_scan, missing_index, inefficient_subquery, implicit_type_conversion,
unbounded_result_set.

Return [] if no issues are found.
"""

_MCP_TOOLS = ",".join([
    "mcp__laravel-boost__database-schema",
    "mcp__laravel-boost__database-query",
    "mcp__laravel-boost__application-info",
])


def extract_php_blocks(diff: str) -> list[dict]:
    """
    Parse a unified diff and return a list of dicts with keys:
      {"file": str, "line": int, "raw": str}
    for PHP files only (.php extension). Only added lines (+ prefix) are
    included. All added lines from the same PHP file are grouped into one block.
    """
    blocks: dict[str, dict] = {}  # file -> {"file", "line", "raw_lines": []}
    current_file: str | None = None
    current_line: int = 0
    hunk_new_start: int = 0
    hunk_new_line: int = 0

    for raw_line in diff.splitlines():
        # Detect file header: +++ b/path/to/file.php
        file_match = re.match(r'^\+\+\+\s+b/(.+)$', raw_line)
        if file_match:
            path = file_match.group(1)
            current_file = path if path.endswith('.php') else None
            hunk_new_start = 0
            hunk_new_line = 0
            continue

        # Detect hunk header: @@ -old_start,old_count +new_start,new_count @@
        hunk_match = re.match(r'^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@', raw_line)
        if hunk_match:
            hunk_new_start = int(hunk_match.group(1))
            hunk_new_line = hunk_new_start
            continue

        if current_file is None:
            # Track line numbers for non-PHP files too (so we skip correctly)
            if raw_line.startswith('+') and not raw_line.startswith('+++'):
                pass
            elif raw_line.startswith('-') and not raw_line.startswith('---'):
                pass
            else:
                if not raw_line.startswith('-'):
                    hunk_new_line += 1
            continue

        # Added line
        if raw_line.startswith('+') and not raw_line.startswith('+++'):
            line_content = raw_line[1:]  # strip leading +
            entry = blocks.setdefault(current_file, {
                "file": current_file,
                "line": hunk_new_line,
                "raw_lines": [],
            })
            entry["raw_lines"].append(line_content)
            hunk_new_line += 1
        elif raw_line.startswith('-') and not raw_line.startswith('---'):
            # Removed line — doesn't advance new-file line counter
            pass
        else:
            # Context line — advances new-file line counter
            hunk_new_line += 1

    result = []
    for entry in blocks.values():
        result.append({
            "file": entry["file"],
            "line": entry["line"],
            "raw": "\n".join(entry["raw_lines"]),
        })
    return result


def write_boost_config(work_dir: str, artisan_path: str) -> str:
    """
    Write a JSON MCP config file for `claude --mcp-config` to work_dir.
    Returns the path to the written config file.
    """
    config = {
        "mcpServers": {
            "laravel-boost": {
                "command": "php",
                "args": [artisan_path, "boost:mcp"],
            }
        }
    }
    config_path = os.path.join(work_dir, "boost-mcp-config.json")
    with open(config_path, "w") as fh:
        json.dump(config, fh, indent=2)
    return config_path


def boost_available(artisan_path: str) -> bool:
    """
    Check whether the Laravel Boost MCP command is available by running
    `php <artisan_path> list --format=json` and looking for "boost:mcp" in stdout.
    Returns False on any exception.
    """
    try:
        proc = subprocess.run(
            ["php", artisan_path, "list", "--format=json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return "boost:mcp" in proc.stdout
    except Exception:
        return False


def call_llm_with_boost(block: dict, config_path: str | None) -> list[dict]:
    """
    Call `claude -p <prompt> [--mcp-config <path> --allowedTools ...]` for the
    given PHP block. Returns a parsed JSON list of review results.

    Falls back to LLM-only (no MCP flags) if config_path is None.
    Returns [] on timeout (120s), non-zero exit, or JSON parse error.
    """
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Review the following PHP/Eloquent code from file `{block['file']}` "
        f"(starting at line {block['line']}):\n\n"
        f"```php\n{block['raw']}\n```\n\n"
        "Return a JSON array of findings as described above. "
        "Return [] if there are no issues."
    )

    cmd = ["claude", "-p", prompt]
    if config_path is not None:
        cmd += ["--mcp-config", config_path, "--allowedTools", _MCP_TOOLS]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if proc.returncode != 0:
            logger.warning(
                "[ORM/LLM] Non-zero exit %d for %s: %s",
                proc.returncode,
                block["file"],
                proc.stderr[:300],
            )
            return []

        output = proc.stdout.strip()
        # Strip markdown fences if present
        cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", output).strip()
        # Find JSON array
        match = re.search(r'\[[\s\S]*\]', cleaned)
        if not match:
            logger.warning("[ORM/LLM] No JSON array in response for %s: %.200s", block["file"], output)
            return []

        return json.loads(match.group(0))

    except subprocess.TimeoutExpired:
        logger.warning("[ORM/LLM] Timed out after 120s for %s", block["file"])
        return []
    except Exception as exc:
        logger.warning("[ORM/LLM] Error reviewing %s: %s", block["file"], exc)
        return []


def review_blocks(
    blocks: list[dict],
    config_path: str | None,
    output_path: str,
) -> None:
    """
    For each block call call_llm_with_boost, collect results, and write an
    artifact JSON file at output_path.

    Output shape:
      [{"query": {"raw": "...", "file": "...", "line": 1, "suppressed": false},
        "result": {...ReviewResult shape...}}]

    If blocks is empty, writes [] immediately.
    """
    if not blocks:
        with open(output_path, "w") as fh:
            json.dump([], fh)
        return

    artifacts = []
    for block in blocks:
        llm_results = call_llm_with_boost(block, config_path)

        # llm_results is a list of result objects; use first, or empty dict
        result = llm_results[0] if llm_results else {}

        artifacts.append({
            "query": {
                "raw": block["raw"],
                "file": block["file"],
                "line": block["line"],
                "suppressed": False,
            },
            "result": result,
        })

    with open(output_path, "w") as fh:
        json.dump(artifacts, fh, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Review PHP/Eloquent ORM code from a PR diff."
    )
    parser.add_argument("--diff", required=True, help="Path to the unified diff file")
    parser.add_argument("--output", required=True, help="Path for the output JSON artifact")
    parser.add_argument(
        "--laravel-path",
        default=None,
        help="Path to the Laravel project root (used to locate artisan)",
    )
    args = parser.parse_args()

    with open(args.diff) as fh:
        diff_text = fh.read()

    blocks = extract_php_blocks(diff_text)
    logger.info("[ORM] Extracted %d PHP block(s) from diff", len(blocks))

    config_path: str | None = None
    if args.laravel_path:
        artisan_path = os.path.join(args.laravel_path, "artisan")
        if boost_available(artisan_path):
            with tempfile.TemporaryDirectory() as work_dir:
                config_path = write_boost_config(work_dir, artisan_path)
                review_blocks(blocks, config_path, args.output)
                return
        else:
            logger.warning("[ORM] Laravel Boost not available at %s — falling back to LLM-only", artisan_path)

    review_blocks(blocks, config_path, args.output)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
