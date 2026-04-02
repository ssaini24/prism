"""LLM prompt templates for DB query optimisation."""
from __future__ import annotations

SYSTEM_PROMPT = """\
You are a senior database engineer and SQL performance specialist.
Your job is to review SQL queries for correctness, performance, and best practices.

You will receive:
- A SQL query extracted from a pull request diff
- Optional schema context (tables, columns, indexes)
- Static analysis findings already identified by rule-based checks

Respond ONLY with a valid JSON object matching this exact schema — no markdown, no prose:

{
  "issues": [
    {
      "type": "string",
      "severity": "low|medium|high",
      "confidence": "low|medium|high",
      "line": 0,
      "description": "string",
      "suggestion": "string"
    }
  ],
  "optimized_query": "string",
  "index_suggestions": ["string"],
  "migration_warnings": ["string"],
  "cost_analysis": {
    "level": "low|medium|high",
    "basis": "static",
    "reason": "string",
    "estimated_improvement": ""
  },
  "explanation": "string",
  "suppressed": []
}

Rules:
- If EXPLAIN data is provided, set basis to "explain" and base cost_analysis on it.
- If no EXPLAIN data, set basis to "static" — never claim runtime data you do not have.
- Only include issues you are confident about; prefer fewer high-confidence findings over many speculative ones.
- optimized_query must be the full rewritten SQL or an empty string if no improvement is possible.
- index_suggestions should be actionable CREATE INDEX statements or empty.
- migration_warnings should flag destructive operations (DROP, TRUNCATE, column removals).
- Keep explanation concise (2-4 sentences max).
- The "type" field MUST be one of the following canonical values — never invent new ones:
  select_star, missing_where_clause, function_on_indexed_column, join_without_condition,
  n_plus_one_pattern, destructive_ddl, unsafe_alter_table, full_table_scan,
  missing_index, inefficient_subquery, implicit_type_conversion, unbounded_result_set
"""


def build_user_prompt(
    query: str,
    schema_context: str,
    static_findings: list[dict],
    explain_result: dict | None = None,
) -> str:
    import json
    parts: list[str] = [f"## SQL Query\n```sql\n{query}\n```"]

    if schema_context:
        parts.append(f"## Schema Context\n```sql\n{schema_context}\n```")

    if explain_result:
        scan_estimates = explain_result.get("scan_estimates", {})
        estimate_lines = []
        for table, est in scan_estimates.items():
            pre = est.get("pre_index_rows", "?")
            total = est.get("total_rows", "?")
            estimate_lines.append(f"- `{table}`: {total} total rows, currently scanning {pre} rows (full scan)")
            for col in est.get("columns", []):
                post = col["post_index_rows"]
                card = col["cardinality"]
                estimate_lines.append(
                    f"  - Adding index on `{col['column']}` (cardinality {card}) → rows scanned: {pre} → ~{post}"
                )

        explain_section = f"## EXPLAIN Output\n```json\n{json.dumps(explain_result, indent=2)}\n```"
        if estimate_lines:
            explain_section += (
                "\n\n## Pre/Post-Index Row Scan Estimates\n"
                + "\n".join(estimate_lines)
                + "\n\nFor each index suggestion, include the exact pre and post row counts from above "
                "in both the `description` field and the `index_suggestions` list. "
                "Format each suggestion as:\n"
                "`CREATE INDEX idx_<table>_<col> ON <table>(<col>);  "
                "-- rows scanned: <pre> → ~<post> (<reduction>% reduction)`"
            )
        else:
            explain_section += (
                "\n\nFor every table with a full scan or missing index, suggest the exact "
                "`CREATE INDEX` statement based on the WHERE, JOIN, and ORDER BY columns."
            )
        parts.append(explain_section)

    if static_findings:
        parts.append(
            f"## Static Analysis Pre-Findings\n```json\n{json.dumps(static_findings, indent=2)}\n```"
        )
    else:
        parts.append("## Static Analysis Pre-Findings\nNone detected by static rules.")

    parts.append("Respond with the JSON review object only.")
    return "\n\n".join(parts)
