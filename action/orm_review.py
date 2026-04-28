"""
PHP/Eloquent ORM reviewer.

Provider selection via LLM_PROVIDER env var:
  anthropic   — Anthropic Python SDK + mcp client for Boost (default; needs ANTHROPIC_API_KEY)
  claude-code — `claude -p` subprocess + --mcp-config for Boost (local dev; no API key needed)
"""
from __future__ import annotations

import sys
import os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile

from action.config_loader import load_config, PrismConfig  # noqa: E402

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a Senior Laravel ORM Specialist reviewing PHP code from a pull request diff.

You have access to live database schema through Laravel Boost MCP tools.

Step 1 — ALWAYS call application-info first (once per review session).
Use it to learn the PHP/Laravel versions, installed packages, and the full list
of Eloquent models in the application. This tells you what models exist so you
can flag references to non-existent models and tailor suggestions to the
installed package versions.

Step 2 — Before flagging any issue involving a table name, column, or index,
call database-schema to verify against the actual migrated schema.

Available tools: database-schema, database-query, application-info.

Objectives:
1. App Context: call application-info to understand the tech stack and model list.
2. Schema Validation: call database-schema to verify tables and columns exist.
   Flag missing tables/columns as severity: high.
3. N+1 Detection: identify loops accessing relationships without eager loading.
   Suggest the correct ->with() call using the actual model relationships.
4. Column Selection: flag select() or pluck() opportunities for large datasets.
5. Performance: suggest chunk(), lazy(), or cursor() over get() where appropriate.
6. Modern Standards: flag non-idiomatic patterns for the installed Laravel version.

Rules:
- Call application-info at the start — never skip it.
- Never assume a table/column/model exists — verify with tools.
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

_DB_ENV_KEYS = (
    "APP_KEY", "DB_CONNECTION", "DB_HOST", "DB_PORT",
    "DB_DATABASE", "DB_USERNAME", "DB_PASSWORD",
)

# ── cost tracking ─────────────────────────────────────────────────────────────

_COST_PER_1K: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6":         (0.003,   0.015),
    "claude-haiku-4-5-20251001": (0.00025, 0.00125),
}

_usage: dict = {"calls": 0, "input_tokens": 0, "output_tokens": 0}


def _track_usage(model: str, input_tokens: int, output_tokens: int) -> None:
    _usage["calls"] += 1
    _usage["input_tokens"] += input_tokens
    _usage["output_tokens"] += output_tokens


def _log_cost_summary(model: str) -> None:
    cost_in, cost_out = _COST_PER_1K.get(model, (0.0, 0.0))
    cost = (_usage["input_tokens"] / 1000 * cost_in) + (_usage["output_tokens"] / 1000 * cost_out)
    logger.info("[ORM] ── Cost Summary ──────────────────────────────────")
    logger.info("[ORM]   model    : %s", model)
    logger.info("[ORM]   calls    : %d", _usage["calls"])
    logger.info("[ORM]   input    : %d tokens", _usage["input_tokens"])
    logger.info("[ORM]   output   : %d tokens", _usage["output_tokens"])
    logger.info("[ORM]   est cost : $%.6f", cost)
    logger.info("[ORM] ───────────────────────────────────────────────────")


def _current_cost_usd(model: str) -> float:
    cost_in, cost_out = _COST_PER_1K.get(model, (0.0, 0.0))
    return (_usage["input_tokens"] / 1000 * cost_in) + (_usage["output_tokens"] / 1000 * cost_out)


# ── diff parsing ──────────────────────────────────────────────────────────────


def _is_trivial_hunk(added_lines: list[str]) -> bool:
    """Return True if every added line is an import, blank line, or docblock — nothing worth reviewing."""
    for line in added_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('use '):
            continue
        if stripped.startswith('//') or stripped.startswith('*') or stripped.startswith('/**') or stripped == '*/':
            continue
        return False
    return True


def _flush_hunk(hunk: "dict | None", blocks: list) -> None:
    """Finalise the current hunk and append a block if it contains reviewable code."""
    if hunk is None or not hunk["lines"]:
        return
    added_lines = [content for (_ln, content, is_added) in hunk["lines"] if is_added]
    if not added_lines or _is_trivial_hunk(added_lines):
        return
    raw = "\n".join(content for (_ln, content, _added) in hunk["lines"])
    first_added = next(
        (ln for ln, _content, is_added in hunk["lines"] if is_added),
        hunk["hunk_start"],
    )
    blocks.append({
        "file": hunk["file"],
        "raw": raw,
        "line": first_added,            # fallback comment anchor — a + line in the diff
        "hunk_start": hunk["hunk_start"],  # absolute line where the hunk begins (for LLM prompt)
    })


def extract_php_blocks(diff: str, prism_config: "PrismConfig | None" = None) -> list[dict]:
    """
    Parse a unified diff and return one block per hunk for PHP files.

    Each block includes context lines alongside added lines so the LLM has
    full structural context (method signatures, class declarations, etc.).
    Trivial hunks that only add imports, blank lines, or docblocks are skipped.
    """
    blocks: list[dict] = []
    current_file: str | None = None
    current_hunk: "dict | None" = None

    for raw_line in diff.splitlines():
        # ── new file header ───────────────────────────────────────────────────
        file_match = re.match(r'^\+\+\+\s+b/(.+)$', raw_line)
        if file_match:
            _flush_hunk(current_hunk, blocks)
            current_hunk = None
            path = file_match.group(1)
            if not path.endswith('.php'):
                current_file = None
                continue
            if prism_config and not prism_config.should_scan(path):
                logger.debug("[ORM] Skipping %s (not in scan_paths)", path)
                current_file = None
                continue
            current_file = path
            continue

        # ── hunk header ───────────────────────────────────────────────────────
        hunk_match = re.match(r'^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@', raw_line)
        if hunk_match:
            _flush_hunk(current_hunk, blocks)
            current_hunk = None
            if current_file is None:
                continue
            hunk_start = int(hunk_match.group(1))
            current_hunk = {
                "file": current_file,
                "hunk_start": hunk_start,
                "current_line": hunk_start,
                "lines": [],  # list of (line_no: int, content: str, is_added: bool)
            }
            continue

        if current_hunk is None:
            continue

        # ── diff body ─────────────────────────────────────────────────────────
        if raw_line.startswith('+') and not raw_line.startswith('+++'):
            ln = current_hunk["current_line"]
            current_hunk["lines"].append((ln, raw_line[1:], True))
            current_hunk["current_line"] += 1
        elif raw_line.startswith('-') and not raw_line.startswith('---'):
            pass  # removed lines: don't add to block, don't advance new-side counter
        else:
            # context line (starts with ' ' or bare newline)
            ln = current_hunk["current_line"]
            content = raw_line[1:] if raw_line.startswith(' ') else raw_line
            current_hunk["lines"].append((ln, content, False))
            current_hunk["current_line"] += 1

    _flush_hunk(current_hunk, blocks)
    return blocks


# ── Boost detection ───────────────────────────────────────────────────────────


def boost_available(artisan_path: str) -> bool:
    """Return True if `php artisan boost:mcp` exists in the target Laravel app."""
    try:
        proc = subprocess.run(
            ["php", artisan_path, "list", "--format=json"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ},
        )
        return "boost:mcp" in proc.stdout
    except Exception:
        return False


# ── claude-code provider ──────────────────────────────────────────────────────


def _write_boost_config(work_dir: str, artisan_path: str) -> str:
    """Write a JSON MCP config for `claude --mcp-config`. Returns the file path."""
    config = {
        "mcpServers": {
            "laravel-boost": {
                "command": "php",
                "args": [artisan_path, "boost:mcp"],
            }
        }
    }
    path = os.path.join(work_dir, "boost-mcp-config.json")
    with open(path, "w") as fh:
        json.dump(config, fh, indent=2)
    return path


def _call_claude_code(block: dict, config_path: str | None, model: str) -> list[dict]:
    """
    Call `claude -p <prompt> [--mcp-config ... --allowedTools ...]`.
    The CLI uses whatever model it is configured with; --model is not passed
    because the CLI uses dated model IDs that differ from the API model names.
    config_path=None means no Boost (LLM-only).
    Token counts are estimated (~4 chars/token) since the CLI doesn't expose usage.
    """
    block_start = block.get("hunk_start", block["line"])
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Review the following PHP/Eloquent code from `{block['file']}` "
        f"(starts at file line {block_start}):\n\n"
        f"```php\n{block['raw']}\n```\n\n"
        "Return a JSON array of findings. Return [] if no issues."
    )

    cmd = ["claude", "-p", prompt]
    if config_path is not None:
        cmd += ["--mcp-config", config_path, "--allowedTools", _MCP_TOOLS]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            logger.warning("[ORM/claude-code] Exit %d for %s: %s",
                           proc.returncode, block["file"],
                           (proc.stderr or proc.stdout)[:300])
            return []
        # Estimate tokens: ~4 chars per token (CLI doesn't expose usage)
        _track_usage(model, len(prompt) // 4, len(proc.stdout) // 4)
        return _parse_json_response(proc.stdout)
    except subprocess.TimeoutExpired:
        logger.warning("[ORM/claude-code] Timed out for %s", block["file"])
        return []
    except Exception as exc:
        logger.warning("[ORM/claude-code] Error for %s: %s", block["file"], exc)
        return []


# ── anthropic provider ────────────────────────────────────────────────────────


def _parse_json_response(text: str) -> list[dict]:
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", text.strip()).strip()
    match = re.search(r'\[[\s\S]*\]', cleaned)
    if not match:
        logger.warning("[ORM] No JSON array in response: %.200s", text)
        return []
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        logger.warning("[ORM] JSON parse error: %s", exc)
        return []


async def _call_anthropic_with_boost(
    block: dict,
    artisan_path: str,
    api_key: str,
    model: str,
    db_env: dict,
) -> list[dict]:
    """Anthropic SDK agentic loop with Boost MCP tools (schema-aware)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import anthropic as sdk

    server_params = StdioServerParameters(
        command="php",
        args=[artisan_path, "boost:mcp"],
        env={**os.environ, **db_env},
    )

    client = sdk.Anthropic(api_key=api_key)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.inputSchema,
                }
                for t in mcp_tools
            ]

            block_start = block.get("hunk_start", block["line"])
            messages: list[dict] = [{
                "role": "user",
                "content": (
                    f"Review this PHP/Eloquent code from `{block['file']}` "
                    f"(starts at file line {block_start}):\n\n```php\n{block['raw']}\n```\n\n"
                    "Return a JSON array of findings as described. Return [] if no issues."
                ),
            }]

            total_in = 0
            total_out = 0
            for _ in range(10):  # max agentic turns
                response = client.messages.create(
                    model=model,
                    max_tokens=2048,
                    system=SYSTEM_PROMPT,
                    messages=messages,
                    tools=tools,
                )
                total_in += response.usage.input_tokens
                total_out += response.usage.output_tokens

                if response.stop_reason == "end_turn":
                    _track_usage(model, total_in, total_out)
                    for cb in response.content:
                        if hasattr(cb, "text"):
                            return _parse_json_response(cb.text)
                    return []

                if response.stop_reason == "tool_use":
                    messages.append({
                        "role": "assistant",
                        "content": [cb.model_dump() for cb in response.content],
                    })
                    tool_results = []
                    for cb in response.content:
                        if cb.type == "tool_use":
                            try:
                                result = await session.call_tool(cb.name, cb.input)
                                result_text = "\n".join(
                                    r.text for r in result.content if hasattr(r, "text")
                                )
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": cb.id,
                                    "content": result_text,
                                })
                            except Exception as exc:
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": cb.id,
                                    "content": f"Tool error: {exc}",
                                    "is_error": True,
                                })
                    messages.append({"role": "user", "content": tool_results})
                else:
                    break

            _track_usage(model, total_in, total_out)
    return []


async def _call_anthropic_no_boost(block: dict, api_key: str, model: str) -> list[dict]:
    """Anthropic SDK call without MCP tools (static-only fallback)."""
    import anthropic as sdk

    client = sdk.Anthropic(api_key=api_key)
    block_start = block.get("hunk_start", block["line"])
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Review this PHP/Eloquent code from `{block['file']}` "
                f"(starts at file line {block_start}):\n\n```php\n{block['raw']}\n```\n\n"
                "Return a JSON array of findings as described. Return [] if no issues."
            ),
        }],
    )
    _track_usage(model, response.usage.input_tokens, response.usage.output_tokens)
    for cb in response.content:
        if hasattr(cb, "text"):
            return _parse_json_response(cb.text)
    return []


# ── orchestration ─────────────────────────────────────────────────────────────


def _call_block(
    block: dict,
    provider: str,
    use_boost: bool,
    artisan_path: str | None,
    config_path: str | None,
    api_key: str | None,
    model: str,
    db_env: dict,
) -> list[dict]:
    try:
        if provider == "claude-code":
            return _call_claude_code(block, config_path if use_boost else None, model)
        else:
            if use_boost and artisan_path:
                return asyncio.run(
                    _call_anthropic_with_boost(block, artisan_path, api_key, model, db_env)
                )
            else:
                return asyncio.run(_call_anthropic_no_boost(block, api_key, model))
    except Exception as exc:
        logger.warning("[ORM] Unexpected error for %s: %s", block["file"], exc)
        return []


def review_blocks(
    blocks: list[dict],
    provider: str,
    use_boost: bool,
    artisan_path: str | None,
    config_path: str | None,
    api_key: str | None,
    model: str,
    output_path: str,
    db_env: dict,
    prism_config: PrismConfig,
    cost_threshold_usd: float,
) -> None:
    if not blocks:
        with open(output_path, "w") as fh:
            json.dump([], fh)
        return

    artifacts = []
    for block in blocks:
        # Cost threshold guard
        running_cost = _current_cost_usd(model)
        if running_cost > cost_threshold_usd:
            logger.warning(
                "[ORM] Cost threshold $%.4f exceeded ($%.4f so far) — stopping after %d/%d blocks",
                cost_threshold_usd, running_cost, len(artifacts), len(blocks),
            )
            break

        llm_results = _call_block(
            block, provider, use_boost, artisan_path, config_path, api_key, model, db_env
        )

        # Filter disabled rules from results
        for item in llm_results:
            if "issues" in item:
                item["issues"] = [
                    issue for issue in item["issues"]
                    if not prism_config.is_rule_disabled(issue.get("type", ""))
                ]

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
    parser = argparse.ArgumentParser(description="Review PHP/Eloquent ORM code from a PR diff.")
    parser.add_argument("--diff", required=True, help="Path to the unified diff file")
    parser.add_argument("--output", required=True, help="Path for the output JSON artifact")
    parser.add_argument("--laravel-path", default=None, help="Laravel project root (locates artisan)")
    args = parser.parse_args()

    provider = os.environ.get("LLM_PROVIDER", "anthropic").lower()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")

    if provider == "anthropic" and not api_key:
        logger.warning("[ORM] LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY not set — skipping analysis")
        with open(args.output, "w") as fh:
            json.dump([], fh)
        return

    db_env = {k: os.environ[k] for k in _DB_ENV_KEYS if k in os.environ}

    prism_config = load_config(args.laravel_path)
    cost_threshold_usd = float(os.environ.get("COST_THRESHOLD_USD", "1.00"))

    with open(args.diff) as fh:
        diff_text = fh.read()

    blocks = extract_php_blocks(diff_text, prism_config=prism_config)
    logger.info("[ORM] Extracted %d PHP block(s) from diff (scan_paths=%s)",
                len(blocks), prism_config.scan_paths)
    logger.info("[ORM] Cost threshold: $%.2f", cost_threshold_usd)

    use_boost = False
    artisan_path = None
    config_path = None
    tmp_dir = None

    if args.laravel_path:
        artisan_path = os.path.join(args.laravel_path, "artisan")
        if boost_available(artisan_path):
            use_boost = True
            logger.info("[ORM] Boost MCP available — schema-aware analysis enabled")
            if provider == "claude-code":
                tmp_dir = tempfile.mkdtemp()
                config_path = _write_boost_config(tmp_dir, artisan_path)
        else:
            logger.warning("[ORM] Boost not available at %s — LLM-only", artisan_path)

    review_blocks(
        blocks, provider, use_boost, artisan_path, config_path,
        api_key, model, args.output, db_env,
        prism_config=prism_config, cost_threshold_usd=cost_threshold_usd,
    )
    _log_cost_summary(model)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
