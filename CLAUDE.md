# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Prism** is an AI-powered pull request reviewer that analyzes SQL queries in GitHub PR diffs, runs static analysis rules, then calls Claude via the Anthropic SDK to generate optimization suggestions. It posts structured review comments (inline + summary) back to the PR.

## Commands

### Running Locally

```bash
cp .env.example .env
# Fill in GITHUB_WEBHOOK_SECRET, GITHUB_TOKEN, ANTHROPIC_API_KEY
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Docker

```bash
docker-compose up --build
```

Health check: `GET /health`

### Linting / Type Checking (not yet configured in repo, but used locally)

```bash
ruff check .
mypy .
```

## Architecture

### Request Flow

```
POST /webhook/github
  → github/webhook.py         # HMAC-SHA256 validation, filter PR events
  → core/analyser.py          # Orchestrates: fetch diff, extract queries, run reviewers
  → core/diff_parser.py       # Extracts SQL from unified git diff
  → reviewers/db_query/       # Static rules + LLM review per extracted query
  → github/commenter.py       # Posts inline comments + summary to PR
```

### Key Layers

| Layer | Path | Responsibility |
|-------|------|----------------|
| HTTP entry | `main.py` | FastAPI app, two endpoints (`/health`, `/webhook/github`) |
| Config | `config/settings.py` | pydantic-settings loading from `.env` |
| Orchestration | `core/analyser.py` | Coordinates diff parsing and reviewer dispatch |
| Diff parsing | `core/diff_parser.py` | Extracts SQL queries + line numbers from diffs |
| LLM wrapper | `core/llm_client.py` | Anthropic SDK calls with JSON output enforcement |
| Reviewer base | `reviewers/base_reviewer.py` | Abstract class all reviewers must implement |
| DB query review | `reviewers/db_query/reviewer.py` | Runs static rules then LLM; returns `ReviewResult` |
| Static rules | `reviewers/db_query/rules.py` | 6 SQL anti-pattern detectors (using sqlglot AST) |
| LLM prompts | `reviewers/db_query/prompts.py` | System and user prompt templates |
| Data models | `models/review.py` | Pydantic schemas for all reviewer output |
| GitHub API | `github/commenter.py` | PyGithub integration for inline + summary comments |

### Adding a New Reviewer

1. Extend `BaseReviewer` from `reviewers/base_reviewer.py`
2. Implement the `review()` method returning a `ReviewResult`
3. Register the reviewer in `core/analyser.py`

### Adding a New Static Rule

Add a function to `reviewers/db_query/rules.py` and call it from `run_all_rules()`.

## Configuration (`.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `GITHUB_WEBHOOK_SECRET` | — | HMAC secret for webhook validation |
| `GITHUB_TOKEN` | — | GitHub PAT for reading diffs and posting comments |
| `ANTHROPIC_API_KEY` | — | Claude API key |
| `LLM_MODEL` | `claude-haiku-4-5-20251001` | Which Claude model to use |
| `LLM_MAX_TOKENS` | `2048` | Max LLM response tokens |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `ENVIRONMENT` | `development` | Environment name |

## Key Behaviors

- **Webhook filtering:** Only `opened`, `synchronize`, `reopened` PR actions are processed; all others return 200 immediately.
- **Query suppression:** SQL lines with `-- prism: ignore` are skipped entirely.
- **Graceful degradation:** If the LLM call fails, static analysis results are still posted. If a query can't be parsed by sqlglot, static rules are skipped and only the LLM sees the raw SQL.
- **PR comments:** Inline comments target the specific file+line of each query; a top-level summary comment aggregates all findings with severity emoji (🟡 low, 🟠 medium, 🔴 high).

## Output Schema

All review output is validated against `models/review.py` (`ReviewResult`). Key fields: `issues[]`, `optimized_query`, `index_suggestions[]`, `migration_warnings[]`, `cost_analysis`, `explanation`.
