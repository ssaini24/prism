# ORM Context-Aware Analysis — Design Spec
**Date:** 2026-04-12
**Status:** Approved

---

## Problem

The current ORM reviewer translates Eloquent code to raw SQL, then analyzes the SQL. This breaks for real-world Laravel PRs where:

- Columns are dynamic (`->select($request->input('fields'))`)
- Queries are nested or built conditionally across multiple methods
- Pagination is chained (`->paginate($request->perPage)`)
- Only a partial diff is available — not the full query builder chain

Without access to the actual database schema, the LLM guesses at table structure, indexes, and relationships. This produces low-confidence, often wrong suggestions.

---

## Solution

**Option A: Reusable GitHub Actions Workflow with Laravel Boost MCP**

Replace the webhook-based ORM reviewer with a reusable GitHub Actions workflow. Target repos call it with a 5-line caller workflow. The reusable workflow:

1. Spins up a real MySQL container
2. Runs the target repo's migrations to build the actual schema
3. Starts Laravel Boost as an MCP server — giving the LLM live access to table schemas, columns, and indexes
4. Runs the existing Python static analyzer in parallel for SQL/non-PHP files
5. Merges results and posts all comments in one pass via the existing Python commenter

No webhook server required in production. GitHub hosts the runners.

---

## Architecture

### Workflow Structure

```
PR opened/synchronize/reopened
    │
    ▼
Target repo: .github/workflows/prism-review.yml
    └── calls: ssaini24/prism/.github/workflows/review.yml@main
                    │
    ┌───────────────┴───────────────────┐
    │                                   │
Job 1: orm-analysis              Job 2: static-analysis
(has MySQL service)              (parallel, no DB needed)
    │                                   │
    ├── Checkout target repo            ├── Checkout Prism repo
    ├── Checkout Prism repo             ├── Fetch PR diff (GitHub API)
    ├── composer install                ├── Run Python Prism (Docker)
    │   (target + laravel/boost)        │   (static rules + LLM)
    ├── php artisan migrate             └── Upload static-results.json
    ├── Fetch PR diff
    ├── For each PHP block:
    │   ├── Spawn Boost MCP subprocess
    │   ├── Prism PHP → LLM tool-call loop
    │   │   LLM ↔ Boost tools (schema/indexes)
    │   └── Collect JSON result
    └── Upload orm-results.json
                    │
                    ▼
            Job 3: post-comments
            (needs: Job 1 + Job 2)
            ├── Download both artifacts
            ├── Deserialize into (ExtractedQuery, ReviewResult) pairs
            └── PRCommenter.post_review() → PR inline + summary
```

---

## New Files in Prism Repo

| Path | Purpose |
|---|---|
| `.github/workflows/review.yml` | The reusable workflow definition |
| `action/review.php` | PHP ORM reviewer script (Prism PHP + Boost) |
| `action/composer.json` | PHP deps: `prism-php/prism`, `prism-php/relay` |
| `action/entrypoint.sh` | Installs deps, runs review.php, writes artifact |
| `action/post_comments.py` | Loads both artifacts, calls existing PRCommenter |

No changes to existing Python files. `commenter.py`, `analyser.py`, and all static rules are unchanged.

> **Naming note:** "Prism PHP" refers to the third-party `prism-php/prism` Laravel library for LLM integration. "Prism" (unqualified) refers to this bot repo (`ssaini24/prism`).

> **Dockerfile note:** Job 2 runs the Python static analyzer via `docker run ssaini24/prism:latest`. This requires adding a CLI entrypoint (`python -m prism.cli analyze --diff <file> --output <file>`) and publishing the Docker image to GHCR. This is a prerequisite step for the implementation.

---

## Target Repo Setup

Target repos add one file:

```yaml
# .github/workflows/prism-review.yml
name: Prism Code Review
on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  review:
    uses: ssaini24/prism/.github/workflows/review.yml@main
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

Requirements in target repo:
- `ANTHROPIC_API_KEY` secret set in GitHub repo settings
- A working Laravel app with `php artisan migrate` runnable in CI
- `.env.ci` or workflow-level DB env vars for the MySQL service container

---

## Prism PHP ↔ Boost MCP Interaction

The interaction is an **agentic tool-calling loop**, not a one-shot prompt:

```
1. PHP script sends ORM code block to LLM via Prism PHP

2. LLM sees: User::where('user_id', $id)->get()
   LLM decides it needs schema context
   LLM returns: tool_call → describe_table("users")

3. Prism Relay translates to MCP protocol → Boost subprocess (stdio)

4. Boost queries live MySQL:
   SHOW COLUMNS FROM users;
   SHOW INDEX FROM users;
   Returns: { columns: [...], indexes: ["id", "email"] }

5. LLM sees no index on user_id → flags missing_index
   LLM calls next tool if needed, or returns final JSON

6. PHP script writes result to orm-results.json artifact
```

Boost runs as a **stdio subprocess** (`php artisan boost:mcp`). No HTTP port management needed in CI.

### Boost Tools Used

| Tool | When LLM calls it |
|---|---|
| `list_tables` | Verify a table referenced in ORM code actually exists |
| `describe_table` | Check columns and types — catch missing column errors |
| `table_indexes` | Detect missing indexes on WHERE/JOIN/ORDER BY columns |

---

## Prompt Design

### System Prompt

Based on the architect's prompt, adapted to output `ReviewResult` JSON schema:

```
# Role: Senior Laravel ORM Specialist & Database Auditor
You are an expert Laravel developer reviewing PR diffs. Your specialty is
Eloquent ORM performance, database integrity, and modern Laravel best practices.

# Contextual Awareness
You have access to live database schema through Laravel Boost MCP tools.
Before flagging any issue involving a table name, column, or index — you MUST
call the relevant tool to verify against the actual migrated schema.

# Objectives
1. Schema Validation: If you see a table name in ORM code, call describe_table.
   If the table is missing or renamed, flag as severity: high.
2. N+1 Detection: Identify loops where relationships are accessed without
   eager loading (missing with()). Always suggest the corrected with() call.
3. Column Selection: Flag select() or pluck() opportunities for large datasets.
4. Performance: Suggest chunk(), lazy(), or cursor() over get() where appropriate.
5. Modern Standards: Flag non-idiomatic Laravel patterns.

# Rules
- Never assume a table or column exists — call a tool to verify.
- Never hallucinate that a migration exists because it is mentioned in a comment.
- Set confidence: low if you could not verify schema due to tool failure.
- Set cost_analysis.basis to "explain" when your findings are backed by
  schema/index data from tools; "static" otherwise.

# Output Format
Respond ONLY with a valid JSON array. No markdown, no prose.
Each element represents one reviewed code block:

[
  {
    "file": "app/Http/Controllers/UserController.php",
    "line": 42,
    "issues": [
      {
        "type": "n_plus_one_pattern",
        "severity": "high",
        "confidence": "high",
        "line": 42,
        "description": "Relationship orders accessed in loop without eager loading.",
        "suggestion": "Add ->with('orders') to the query before the loop."
      }
    ],
    "optimized_query": "User::with('orders')->where('active', true)->get()",
    "index_suggestions": [
      "CREATE INDEX idx_orders_user_id ON orders(user_id);"
    ],
    "migration_warnings": [],
    "cost_analysis": {
      "level": "high",
      "basis": "explain",
      "reason": "No index on orders.user_id — full scan on every loop iteration.",
      "estimated_improvement": "~99% row reduction with index"
    },
    "explanation": "2-3 sentence summary of findings."
  }
]

Valid issue types: select_star, missing_where_clause, function_on_indexed_column,
join_without_condition, n_plus_one_pattern, destructive_ddl, unsafe_alter_table,
full_table_scan, missing_index, inefficient_subquery, implicit_type_conversion,
unbounded_result_set
```

---

## Results Artifact Format

Both jobs produce a JSON file in this shape, matching existing Pydantic models:

```json
[
  {
    "query": {
      "raw": "User::with('orders')->where('active', true)->get()",
      "file": "app/Http/Controllers/UserController.php",
      "line": 42,
      "suppressed": false
    },
    "result": {
      "issues": [...],
      "optimized_query": "...",
      "index_suggestions": [...],
      "migration_warnings": [...],
      "cost_analysis": { "level": "high", "basis": "explain", ... },
      "explanation": "..."
    }
  }
]
```

`action/post_comments.py` deserializes both artifacts into `(ExtractedQuery, ReviewResult)` pairs and passes them to the existing `PRCommenter.post_review()`. No changes to commenter logic.

---

## Error Handling

| Failure | Behaviour |
|---|---|
| Migration fails | Job 1 exits; Job 2 still runs; summary comment notes "ORM analysis skipped — migration failed" |
| Boost fails to start | Fall back to LLM-only (no tools); all issues get `confidence: low` |
| LLM call fails for a block | Skip block, log warning, continue remaining blocks |
| No PHP files in diff | Job 1 exits early with empty artifact |
| Job 2 (static) fails | Job 3 still posts ORM results if artifact exists |
| Both jobs produce 0 issues | Post existing clean bill of health comment |
| `ANTHROPIC_API_KEY` missing | Workflow fails fast with clear error message in Actions log |

---

## Out of Scope

- Support for ORM frameworks other than Eloquent (Rails, Django ORM, TypeORM)
- Runtime query execution or `EXPLAIN` via Boost (schema inspection only)
- Caching migrated schemas across PRs
- Support for multi-database Laravel apps

---

## Open Questions

1. Does `laravel/boost` expose `describe_table` and `table_indexes` as distinct MCP tools, or is schema returned as a single tool? Verify against Boost package docs before implementing.
2. Does Prism PHP Relay support stdio MCP transport, or does Boost need to run as HTTP? Verify against `prism-php/relay` docs.
3. Should the caller workflow support PostgreSQL service containers, or MySQL only for v1?
