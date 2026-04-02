"""Static analysis rules for SQL query review."""
from __future__ import annotations

import re

import sqlglot
import sqlglot.expressions as exp

from models.review import Issue


def run_all_rules(query: str, indexed_columns: set[str] | None = None) -> list[Issue]:
    """
    Run all static rules against the given SQL string.

    Args:
        query: Raw SQL text.
        indexed_columns: Set of column names known to be indexed (from schema).
                         If None, index-related rules use heuristics only.

    Returns:
        List of Issue objects found by rule-based analysis.
    """
    indexed_columns = indexed_columns or set()
    issues: list[Issue] = []

    try:
        statements = sqlglot.parse(query, dialect="mysql", error_level=sqlglot.ErrorLevel.WARN)
    except Exception:
        # Unparseable — skip static rules, let LLM handle it
        return issues

    for stmt in statements:
        if stmt is None:
            continue
        issues.extend(_check_select_star(stmt))
        issues.extend(_check_missing_where(stmt))
        issues.extend(_check_functions_on_indexed_columns(stmt, indexed_columns))
        issues.extend(_check_inefficient_joins(stmt))
        issues.extend(_check_n_plus_one_heuristic(stmt))
        issues.extend(_check_migration_risks(stmt))
        issues.extend(_check_unsafe_alter(stmt))

    return issues


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------


def _check_select_star(stmt: exp.Expression) -> list[Issue]:
    issues = []
    for node in stmt.find_all(exp.Star):
        # Make sure it's inside a SELECT, not e.g. COUNT(*)
        select = node.find_ancestor(exp.Select)
        if select is None:
            continue
        # COUNT(*) is fine
        if node.find_ancestor(exp.Anonymous, exp.Count):
            continue
        issues.append(
            Issue(
                type="select_star",
                severity="medium",
                confidence="high",
                line=0,
                description="SELECT * retrieves all columns, including unused ones. "
                "This wastes bandwidth, prevents index-only scans, and breaks "
                "if the table schema changes.",
                suggestion="Enumerate only the columns your application actually needs.",
            )
        )
    return issues


def _check_missing_where(stmt: exp.Expression) -> list[Issue]:
    issues = []
    for node in stmt.find_all(exp.Update, exp.Delete):
        where = node.find(exp.Where)
        if where is None:
            op = "UPDATE" if isinstance(node, exp.Update) else "DELETE"
            issues.append(
                Issue(
                    type="missing_where_clause",
                    severity="high",
                    confidence="high",
                    line=0,
                    description=f"{op} statement has no WHERE clause — this will affect every row in the table.",
                    suggestion=f"Add a WHERE clause to target only the intended rows, or use LIMIT 1 as a safeguard.",
                )
            )
    return issues


def _check_functions_on_indexed_columns(
    stmt: exp.Expression, indexed_columns: set[str]
) -> list[Issue]:
    """Detect function calls wrapping column references that are likely indexed."""
    issues = []
    # Common functions that invalidate index usage when applied to a column
    invalidating_functions = {
        "lower", "upper", "trim", "ltrim", "rtrim", "date",
        "year", "month", "day", "cast", "convert", "coalesce",
        "ifnull", "isnull", "nvl", "to_date", "to_char",
    }

    for func in stmt.find_all(exp.Anonymous, exp.Func):
        func_name = (
            func.name.lower() if hasattr(func, "name") and func.name else ""
        )
        if func_name not in invalidating_functions:
            continue
        for col in func.find_all(exp.Column):
            col_name = col.name.lower() if col.name else ""
            # Flag if we know it's indexed, or if it appears in a WHERE/JOIN ON
            in_filter = bool(col.find_ancestor(exp.Where, exp.Join))
            if col_name in indexed_columns or in_filter:
                issues.append(
                    Issue(
                        type="function_on_indexed_column",
                        severity="medium",
                        confidence="medium",
                        line=0,
                        description=f"Applying `{func_name}()` to column `{col_name}` in a filter "
                        "prevents the database from using an index on that column.",
                        suggestion=f"Rewrite the condition to avoid wrapping `{col_name}` in a function. "
                        "Consider a functional index if the transformation is unavoidable.",
                    )
                )
    return issues


def _check_inefficient_joins(stmt: exp.Expression) -> list[Issue]:
    """Detect JOINs that lack an ON condition (cross-join risk) or join on non-indexed columns (heuristic)."""
    issues = []
    for join in stmt.find_all(exp.Join):
        on_clause = join.args.get("on")
        using_clause = join.args.get("using")
        is_cross = join.args.get("kind", "").upper() == "CROSS" if join.args.get("kind") else False

        if not on_clause and not using_clause and not is_cross:
            table_name = ""
            if join.this:
                table_name = join.this.name if hasattr(join.this, "name") else str(join.this)
            issues.append(
                Issue(
                    type="join_without_condition",
                    severity="high",
                    confidence="high",
                    line=0,
                    description=f"JOIN on `{table_name}` has no ON or USING clause — this produces a cartesian product.",
                    suggestion="Add an explicit ON condition to define the join predicate.",
                )
            )
    return issues


def _check_n_plus_one_heuristic(stmt: exp.Expression) -> list[Issue]:
    """
    Heuristic: detect correlated subqueries in SELECT or WHERE that execute
    once per outer row — a common N+1 pattern in ORM-generated SQL.
    """
    issues = []
    for subquery in stmt.find_all(exp.Subquery):
        parent = subquery.parent
        # Subquery in SELECT list or WHERE clause
        if not isinstance(parent, (exp.Select, exp.Where, exp.EQ, exp.In)):
            continue
        # Check if the subquery references a column from an outer table
        outer_cols = {col.table for col in subquery.find_all(exp.Column) if col.table}
        inner_tables = {
            t.name
            for t in subquery.find_all(exp.Table)
            if hasattr(t, "name")
        }
        correlated = outer_cols - inner_tables
        if correlated:
            issues.append(
                Issue(
                    type="n_plus_one_pattern",
                    severity="high",
                    confidence="medium",
                    line=0,
                    description="Correlated subquery detected — this executes once per row of the outer query, "
                    "which may cause N+1 performance issues at scale.",
                    suggestion="Rewrite using a JOIN or a lateral join to execute the subquery once.",
                )
            )
    return issues


def _check_migration_risks(stmt: exp.Expression) -> list[Issue]:
    """Flag destructive DDL operations that should be reviewed carefully."""
    issues = []
    if isinstance(stmt, exp.Drop):
        kind = stmt.args.get("kind", "").upper()
        name = stmt.this.name if stmt.this and hasattr(stmt.this, "name") else "unknown"
        issues.append(
            Issue(
                type="destructive_ddl",
                severity="high",
                confidence="high",
                line=0,
                description=f"DROP {kind} `{name}` is irreversible without a backup or migration rollback plan.",
                suggestion="Ensure a rollback migration exists. Consider renaming instead of dropping during initial deployment.",
            )
        )
    if isinstance(stmt, (exp.TruncateTable,)):
        issues.append(
            Issue(
                type="destructive_ddl",
                severity="high",
                confidence="high",
                line=0,
                description="TRUNCATE removes all rows without logging individual row deletions — it cannot be rolled back in some databases.",
                suggestion="Prefer DELETE with a WHERE clause if partial deletion is intended, or ensure this truncation is intentional.",
            )
        )
    return issues


def _check_unsafe_alter(stmt: exp.Expression) -> list[Issue]:
    """
    Flag ALTER TABLE statements that omit ALGORITHM or LOCK options.

    Enriches the warning with the actual row count from MySQL so engineers
    can see estimated lock time before merging.
    """
    issues = []
    if not isinstance(stmt, exp.Alter):
        return issues

    raw = stmt.sql(dialect="mysql").upper()

    has_algorithm = "ALGORITHM=" in raw
    has_lock = "LOCK=" in raw

    if not has_algorithm or not has_lock:
        missing = []
        if not has_algorithm:
            missing.append("ALGORITHM=INPLACE (or INSTANT)")
        if not has_lock:
            missing.append("LOCK=NONE")

        # Extract table name for row count lookup
        table_name = stmt.this.name if stmt.this and hasattr(stmt.this, "name") else None
        row_info = _fetch_table_row_info(table_name) if table_name else None

        if row_info:
            rows, lock_estimate = row_info
            rows_fmt = f"{rows:,}"
            lock_line = (
                f" Table `{table_name}` currently has **{rows_fmt} rows** — "
                f"estimated lock time: **~{lock_estimate}**."
            )
        else:
            lock_line = ""

        issues.append(
            Issue(
                type="unsafe_alter_table",
                severity="high",
                confidence="high",
                line=0,
                description=(
                    f"ALTER TABLE is missing {' and '.join(missing)}. "
                    "Without these options MySQL may acquire a full metadata lock (MDL), "
                    f"blocking all reads and writes for the duration of the migration.{lock_line}"
                ),
                suggestion=(
                    "Add ALGORITHM=INPLACE, LOCK=NONE to allow online DDL with minimal locking. "
                    "Use ALGORITHM=INSTANT for supported operations (MySQL 8.0+). "
                    "If the operation does not support INPLACE, schedule during a maintenance window."
                ),
            )
        )
    return issues


def _fetch_table_row_info(table_name: str) -> tuple[int, str] | None:
    """Return (row_count, lock_estimate) for a table via MySQL MCP."""
    import json, re, subprocess
    prompt = (
        f"Use the mysql MCP tool to run this query and return only a JSON object, no explanation:\n"
        f"SELECT COUNT(*) as cnt FROM `{table_name}`\n"
        f'Return: {{"cnt": <integer>}}'
    )
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", "mcp__mysql__mysql_query"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        match = re.search(r'\{[\s\S]+\}', proc.stdout)
        if not match:
            return None
        rows = int(json.loads(match.group(0)).get("cnt", 0))
        if rows < 10_000:
            estimate = "< 5 seconds"
        elif rows < 100_000:
            estimate = "~10–30 seconds"
        elif rows < 1_000_000:
            estimate = "~1–5 minutes"
        elif rows < 10_000_000:
            estimate = "~10–30 minutes"
        else:
            estimate = "potentially hours — plan a maintenance window"
        return rows, estimate
    except Exception as exc:
        logger.debug("MCP row count failed for `%s`: %s", table_name, exc)
        return None
