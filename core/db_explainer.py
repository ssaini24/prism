"""
Runs EXPLAIN on SQL queries and computes pre/post-index row scan estimates
via the MySQL MCP server (claude -p with mcp__mysql__mysql_query tool).
"""
from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import time

logger = logging.getLogger(__name__)

_EXPLAINABLE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_UNSAFE = re.compile(r"^\s*(DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE)\b", re.IGNORECASE)


class ExplainResult:
    def __init__(
        self,
        rows: list[dict],
        warnings: list[str],
        scan_estimates: dict | None = None,
    ) -> None:
        self.rows = rows
        self.warnings = warnings
        self.scan_estimates: dict = scan_estimates or {}   # {table: {pre, post, cols}}
        self.full_scans: list[str] = []
        self.missing_indexes: list[str] = []
        self.filesorts: list[str] = []
        self.temp_tables: list[str] = []
        self._analyse()

    def _analyse(self) -> None:
        for row in self.rows:
            table = row.get("table", "unknown")
            access_type = (row.get("type") or "").lower()
            key = row.get("key")
            extra = (row.get("Extra") or "").lower()

            if access_type == "all":
                self.full_scans.append(table)
            if not key and access_type not in ("system", "const", "null"):
                self.missing_indexes.append(table)
            if "filesort" in extra:
                self.filesorts.append(table)
            if "temporary" in extra:
                self.temp_tables.append(table)

    def has_issues(self) -> bool:
        return bool(self.full_scans or self.missing_indexes or self.filesorts or self.temp_tables)

    def summary(self) -> str:
        parts = []
        if self.full_scans:
            parts.append(f"Full table scan on: {', '.join(self.full_scans)}")
        if self.missing_indexes:
            parts.append(f"No index used on: {', '.join(self.missing_indexes)}")
        if self.filesorts:
            parts.append(f"Using filesort on: {', '.join(self.filesorts)}")
        if self.temp_tables:
            parts.append(f"Using temporary table on: {', '.join(self.temp_tables)}")
        return " | ".join(parts) if parts else "No issues detected by EXPLAIN."

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "full_scans": self.full_scans,
            "missing_indexes": self.missing_indexes,
            "filesorts": self.filesorts,
            "temp_tables": self.temp_tables,
            "scan_estimates": self.scan_estimates,
            "summary": self.summary(),
        }


# ---------------------------------------------------------------------------
# Backend: MCP (claude-code provider)
# ---------------------------------------------------------------------------


def explain_via_mcp(sql: str) -> ExplainResult | None:
    """Run EXPLAIN + cardinality queries via the MySQL MCP server."""
    if _UNSAFE.match(sql) or not _EXPLAINABLE.match(sql):
        return None

    short = sql.strip()[:80].replace("\n", " ")
    logger.info("[EXPLAIN/MCP] ▶ Running via MySQL MCP server: %s...", short)

    # Ask Claude to run EXPLAIN and the cardinality queries in one shot
    prompt = (
        "You are a database tool runner. Use the mysql MCP tool to run these SQL statements "
        "and return results as a single JSON object — no markdown, no explanation.\n\n"
        f"1. EXPLAIN {sql.strip()}\n\n"
        "Then for each table that has type=ALL in the EXPLAIN output (full table scan):\n"
        "2. SELECT COUNT(*) as total_rows FROM <table>\n"
        "3. SELECT COUNT(DISTINCT <col>) as cardinality FROM <table> "
        "for each column referenced in WHERE or JOIN ON clauses\n\n"
        "Return this exact JSON shape:\n"
        "{\n"
        '  "explain_rows": [<each EXPLAIN row as an object>],\n'
        '  "cardinality": {\n'
        '    "<table_name>": {\n'
        '      "total_rows": <integer>,\n'
        '      "columns": {"<col_name>": <distinct_count>, ...}\n'
        "    }\n"
        "  }\n"
        "}"
    )

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", "mcp__mysql__mysql_query"],
            capture_output=True, text=True, timeout=60,
        )
        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            logger.warning("[EXPLAIN/MCP] ✗ Failed (exit %d, %.1fs): %s",
                           proc.returncode, elapsed, proc.stderr[:300])
            return None

        output = proc.stdout.strip()
        cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", output).strip()
        match = re.search(r'\{[\s\S]+\}', cleaned)
        if not match:
            logger.warning("[EXPLAIN/MCP] ✗ No JSON in response: %.200s", output)
            return None

        data = json.loads(match.group(0))
        explain_rows = data.get("explain_rows", [])
        cardinality_data = data.get("cardinality", {})

        scan_estimates = _compute_scan_estimates(explain_rows, cardinality_data)
        result = ExplainResult(rows=explain_rows, warnings=[], scan_estimates=scan_estimates)

        logger.info("[EXPLAIN/MCP] ✓ Done in %.1fs — %s", elapsed, result.summary())
        _log_scan_estimates(scan_estimates)
        return result

    except subprocess.TimeoutExpired:
        logger.warning("[EXPLAIN/MCP] ✗ Timed out after 60s.")
        return None
    except Exception as exc:
        logger.warning("[EXPLAIN/MCP] ✗ Error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_where_columns(sql: str) -> dict[str, list[str]]:
    """Return {table_or_alias: [col, ...]} for columns in WHERE / JOIN ON clauses."""
    try:
        import sqlglot
        import sqlglot.expressions as exp

        stmt = sqlglot.parse_one(sql, dialect="mysql")
        where_cols: dict[str, list[str]] = {}
        for col in stmt.find_all(exp.Column):
            if col.find_ancestor(exp.Where, exp.Join):
                table = col.table or ""
                name = col.name
                if name:
                    where_cols.setdefault(table, []).append(name)
        return where_cols
    except Exception:
        return {}


def _compute_scan_estimates(explain_rows: list[dict], cardinality_data: dict) -> dict:
    """
    Build per-table scan estimate dicts:
      {table: {pre_index_rows, total_rows, columns: [{column, cardinality, post_index_rows}]}}
    """
    estimates: dict = {}
    for row in explain_rows:
        table = row.get("table", "")
        if not table or (row.get("type") or "").lower() != "all":
            continue

        pre_rows = row.get("rows", 0) or 0
        card = cardinality_data.get(table, {})
        total_rows = card.get("total_rows", pre_rows)
        col_data = card.get("columns", {})

        col_estimates = []
        for col, distinct_count in col_data.items():
            post_rows = max(1, math.ceil(total_rows / max(distinct_count, 1)))
            col_estimates.append({
                "column": col,
                "cardinality": distinct_count,
                "post_index_rows": post_rows,
            })

        estimates[table] = {
            "pre_index_rows": pre_rows,
            "total_rows": total_rows,
            "columns": col_estimates,
        }

    return estimates


def _log_scan_estimates(scan_estimates: dict) -> None:
    for table, est in scan_estimates.items():
        pre = est["pre_index_rows"]
        total = est["total_rows"]
        logger.info("[EXPLAIN] Table `%s`: %d total rows, scanning %d pre-index", table, total, pre)
        for col in est.get("columns", []):
            post = col["post_index_rows"]
            card = col["cardinality"]
            reduction = round((1 - post / max(pre, 1)) * 100)
            logger.info(
                "[EXPLAIN]   col %-20s  cardinality: %5d  |  rows: %d → ~%d  (%d%% reduction)",
                f"`{col['column']}`", card, pre, post, reduction,
            )
