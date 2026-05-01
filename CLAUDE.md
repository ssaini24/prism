# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Prism** is a GitHub Actions-based AI PR reviewer with two parallel analysis pipelines: an ORM reviewer for PHP/Laravel files (schema-aware via Laravel Boost MCP) and a static SQL reviewer for SQL embedded in any code file. Results from both pipelines are merged and posted as inline PR comments.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
pytest tests/

# Run a single test file
pytest tests/action/test_orm_review.py -v

# Lint / type-check (configured locally, not in CI)
ruff check .
mypy .
```

There is no local server to run. The primary deployment is via the reusable GitHub Actions workflow at `.github/workflows/review.yml`.

## Architecture

### Two Pipelines, Three Jobs

The CI workflow runs three jobs: `orm-analysis` and `static-analysis` in parallel, then `post-comments` after both.

**ORM pipeline** (`action/orm_review.py`):
- Parses PHP files from the diff into per-method blocks (`extract_php_blocks`)
- Sends each method block to Claude (either via `claude -p` subprocess or Anthropic SDK with Laravel Boost MCP)
- Boost MCP provides live schema introspection (`database-schema`, `database-query`, `application-info`) from the target repo's running MySQL instance
- Writes `orm-results.json`

**Static analysis pipeline** (`action/analyze.py` → `core/analyser.py`):
- `core/diff_parser.py` extracts SQL-containing lines from the diff (`_SQL_KEYWORDS` regex + sqlglot consolidation)
- `reviewers/db_query/reviewer.py` runs 7 static rules (sqlglot AST) then calls LLM for optimization suggestions
- Writes `static-results.json`

**Post-comments** (`action/post_comments.py`):
- Loads both artifact JSONs, deserializes into `(ExtractedQuery, ReviewResult)` pairs
- Calls `gh/commenter.py` → `PRCommenter.post_review()`

### Diff Parsing for ORM (`action/orm_review.py`)

The diff format from the GitHub API includes explicit `--- a/` and `+++ b/` file headers (no `diff --git` prefix). The parser:
- Flushes the current hunk on `--- ` lines (prevents inter-file content bleed)
- Processes one block per `@@` hunk
- Includes context lines (unchanged) alongside `+` lines so the LLM sees method signatures
- Skips trivial hunks (only `use` statements / blank lines / docblocks)
- Splits hunks with ≥2 methods at method signature boundaries (`_split_by_method`)
- Each block: `block["line"]` = first `+` line (fallback comment anchor); `block["hunk_start"]` = method signature line (reference given to LLM in prompt)

### Inline Comment Posting (`gh/commenter.py`)

- Tries `issue.line` (LLM-reported line) first; retries with `query.line` fallback on GitHub 422 (line not in diff)
- Deduplicates on `(path, diff-position, issue_type)` — posts then immediately deletes if the same issue already exists from a prior run
- Auto-resolves stale comments: existing Prism comments whose issue key wasn't re-raised in the new run are deleted
- Caps at **2 issues per code block** (highest severity first), deduplicated across all reviewers

### Data Flow

```
GitHub Actions
  orm-analysis job:
    action/orm_review.py
      → extract_php_blocks()         # per-method blocks from diff
      → _call_claude_code() or
        _call_anthropic_with_boost()  # LLM + optional Boost MCP
      → orm-results.json

  static-analysis job:
    action/analyze.py
      → core/analyser.py
          → core/diff_parser.py      # SQL extraction
          → reviewers/db_query/      # static rules + LLM
      → static-results.json

  post-comments job:
    action/post_comments.py
      → load_artifact() × 2         # deserialize both JSONs
      → gh/commenter.py             # PRCommenter.post_review()
```

### Key Layers

| Layer | Path | Responsibility |
|-------|------|----------------|
| ORM reviewer | `action/orm_review.py` | PHP diff parsing, per-method blocks, LLM calls with optional Boost MCP |
| Static CLI | `action/analyze.py` | Thin CLI wrapper around `Analyser` for CI |
| Post-comments CLI | `action/post_comments.py` | Merges artifact JSONs, drives `PRCommenter` |
| Config loader | `action/config_loader.py` | Reads `.prism/config.yml` from target repo |
| Orchestration | `core/analyser.py` | Runs SQL reviewers in parallel via `ThreadPoolExecutor` |
| Diff parsing (SQL) | `core/diff_parser.py` | Extracts SQL queries + line numbers from diffs |
| LLM wrapper | `core/llm_client.py` | Providers: `anthropic`, `openai`, `claude-code` (subprocess) |
| DB query reviewer | `reviewers/db_query/reviewer.py` | Static rules → LLM; returns `ReviewResult` |
| Static rules | `reviewers/db_query/rules.py` | 7 SQL anti-pattern detectors using sqlglot AST |
| Commenter | `gh/commenter.py` | PyGithub inline comments with dedup and stale-resolution |
| Data models | `models/review.py` | Pydantic: `ExtractedQuery`, `ReviewResult`, `Issue`, `CostAnalysis` |

### Adding a New Reviewer

1. Extend `BaseReviewer` (`reviewers/base_reviewer.py`)
2. Implement `can_review(query)` and `review(query, schema_context)` returning `ReviewResult`
3. Register in `core/analyser.py` — it will be included in the parallel `_run()` dispatch

### Adding a New Static Rule

Add a function to `reviewers/db_query/rules.py` and call it from `run_all_rules()`. Rules receive a sqlglot AST and return `list[Issue]`.

## Configuration

### Prism Repo (`.env` / CI environment variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `GITHUB_TOKEN` | — | PAT or `github.token` for posting comments |
| `ANTHROPIC_API_KEY` | — | Required when `LLM_PROVIDER=anthropic` |
| `LLM_PROVIDER` | `claude-code` | `claude-code` (subprocess), `anthropic`, or `openai` |
| `LLM_MODEL` | `claude-sonnet-4-6` | Model for static analysis |
| `LLM_MAX_TOKENS` | `2048` | Max tokens per LLM response |
| `COST_THRESHOLD_USD` | `1.00` | ORM reviewer stops after this spend per PR |

### Target Repo (`.prism/config.yml`)

Target repos can commit `.prism/config.yml` to control scan behaviour. `action/config_loader.py` loads this file from `--laravel-path`. See `docs/prism-config-example.yml` for the full schema. Supported keys:

- `scan_paths` — list of glob patterns (default: `["app/**", "database/migrations"]`)
- `disabled_rules` — list of issue type strings to suppress (default: `[]`)

## Key Behaviours

- **Query suppression:** SQL lines with `-- prism: ignore` are skipped entirely.
- **Graceful degradation:** If the LLM call fails, static analysis results are still posted. Sqlglot parse failures skip static rules but still send raw SQL to the LLM.
- **ORM LLM provider selection:** `claude-code` uses `claude -p` subprocess (no API key); `anthropic` uses the SDK and can enable Boost MCP for schema introspection when `boost:mcp` is available in the target artisan.
- **PHP file routing:** The ORM reviewer owns `.php` files; `DBQueryReviewer` explicitly skips them.
