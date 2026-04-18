"""
PHP/Eloquent ORM reviewer.

Provider selection via LLM_PROVIDER env var:
  anthropic   — Anthropic Python SDK + mcp client for Boost (default; needs ANTHROPIC_API_KEY)
  claude-code — `claude -p` subprocess + --mcp-config for Boost (local dev; no API key needed)
"""
from __future__ import annotations

import argparse
import asyncio
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


# ── diff parsing ──────────────────────────────────────────────────────────────


def _split_into_method_blocks(file: str, raw: str, base_line: int) -> list[dict]:
    """
    Split a PHP file's added content into per-method blocks.
    Each block is prepended with the file header (namespace, imports, class
    declaration) so the LLM has full context for every method.
    Falls back to a single block when fewer than 2 methods are found.
    """
    method_re = re.compile(
        r'(?m)^[ \t]{0,4}(?:public|protected|private)\s+(?:static\s+)?function\s+\w+'
    )
    matches = list(method_re.finditer(raw))

    if len(matches) < 2:
        return [{"file": file, "raw": raw, "line": base_line}]

    header = raw[:matches[0].start()].rstrip()
    result = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        method_body = raw[start:end].strip()
        method_line = base_line + raw[:start].count('\n')
        full_block = f"{header}\n\n{method_body}" if header else method_body
        result.append({"file": file, "raw": full_block, "line": method_line})

    return result


def extract_php_blocks(diff: str) -> list[dict]:
    """
    Parse a unified diff and return per-method blocks for PHP files.
    Files with a single method (or no methods) are returned as one block.
    """
    files: dict[str, dict] = {}
    current_file: str | None = None
    hunk_new_line: int = 0

    for raw_line in diff.splitlines():
        file_match = re.match(r'^\+\+\+\s+b/(.+)$', raw_line)
        if file_match:
            path = file_match.group(1)
            current_file = path if path.endswith('.php') else None
            hunk_new_line = 0
            continue

        hunk_match = re.match(r'^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@', raw_line)
        if hunk_match:
            hunk_new_line = int(hunk_match.group(1))
            continue

        if current_file is None:
            if not raw_line.startswith('-'):
                hunk_new_line += 1
            continue

        if raw_line.startswith('+') and not raw_line.startswith('+++'):
            entry = files.setdefault(current_file, {
                "file": current_file,
                "line": hunk_new_line,
                "raw_lines": [],
            })
            entry["raw_lines"].append(raw_line[1:])
            hunk_new_line += 1
        elif raw_line.startswith('-') and not raw_line.startswith('---'):
            pass
        else:
            hunk_new_line += 1

    result = []
    for entry in files.values():
        raw = "\n".join(entry["raw_lines"])
        result.extend(_split_into_method_blocks(entry["file"], raw, entry["line"]))
    return result


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
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Review the following PHP/Eloquent code from `{block['file']}` "
        f"(line {block['line']}):\n\n"
        f"```php\n{block['raw']}\n```\n\n"
        "Return a JSON array of findings. Return [] if no issues."
    )

    cmd = ["claude", "-p", prompt]
    if config_path is not None:
        cmd += ["--mcp-config", config_path, "--allowedTools", _MCP_TOOLS]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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

            messages: list[dict] = [{
                "role": "user",
                "content": (
                    f"Review this PHP/Eloquent code from `{block['file']}` "
                    f"(line {block['line']}):\n\n```php\n{block['raw']}\n```\n\n"
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
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Review this PHP/Eloquent code from `{block['file']}` "
                f"(line {block['line']}):\n\n```php\n{block['raw']}\n```\n\n"
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
) -> None:
    if not blocks:
        with open(output_path, "w") as fh:
            json.dump([], fh)
        return

    artifacts = []
    for block in blocks:
        llm_results = _call_block(
            block, provider, use_boost, artisan_path, config_path, api_key, model, db_env
        )
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

    with open(args.diff) as fh:
        diff_text = fh.read()

    blocks = extract_php_blocks(diff_text)
    logger.info("[ORM] Extracted %d PHP block(s) from diff", len(blocks))

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
    )
    _log_cost_summary(model)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
