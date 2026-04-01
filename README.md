# Prism

AI-powered PR reviewer — extensible, domain-specialist architecture.

## What it does

Prism listens to GitHub PR webhooks, detects SQL query issues in the PR diff, and posts structured review comments (inline + summary) back to the PR. Built as a platform for purpose-built AI reviewers — today SQL, tomorrow security, IaC, compliance, and more.

## Data flow

```
GitHub webhook → main.py → gh/webhook.py (HMAC-SHA256 verify)
                         → Analyser → diff_parser (extract SQL)
                                    → DBQueryReviewer
                                        → rules.py (6 static checks)
                                        → LLMClient (OpenAI / Anthropic)
                                    → ReviewResult (strict JSON)
                         → PRCommenter → inline + summary PR comment
```

## Project structure

```
prism/
├── main.py                        # FastAPI entry point + /health endpoint
├── requirements.txt
├── Dockerfile                     # Multi-stage build (python:3.11-slim)
├── docker-compose.yml
├── .env.example
│
├── core/
│   ├── analyser.py                # Orchestrator — routes PR diff to reviewers
│   ├── diff_parser.py             # Extracts SQL queries from unified diffs
│   └── llm_client.py              # Multi-provider LLM client (OpenAI / Anthropic)
│
├── reviewers/
│   ├── base_reviewer.py           # Abstract base class for all reviewers
│   └── db_query/
│       ├── reviewer.py            # DB query reviewer entry point
│       ├── rules.py               # Static analysis rules
│       ├── explain_parser.py      # Phase 2: EXPLAIN JSON parser (placeholder)
│       └── prompts.py             # LLM prompt templates
│
├── gh/
│   ├── webhook.py                 # Webhook receiver + HMAC-SHA256 validation
│   └── commenter.py               # Posts inline + summary review comments
│
├── models/
│   └── review.py                  # Pydantic models for strict JSON output schema
│
└── config/
    └── settings.py                # pydantic-settings config (loads from .env)
```

## Static analysis rules

| Rule | Severity |
|---|---|
| `SELECT *` detection | medium |
| `UPDATE`/`DELETE` without `WHERE` | high |
| Functions on indexed columns (invalidates index scans) | medium |
| `JOIN` without `ON` condition (cartesian product) | high |
| Correlated subqueries — N+1 heuristic | high |
| Destructive DDL (`DROP`, `TRUNCATE`) | high |
| `ALTER TABLE` without `ALGORITHM` / `LOCK` options (MDL lock risk) | high |

## Tech stack

- Python 3.11+
- FastAPI — webhook server
- sqlglot — SQL AST parsing
- PyGithub — GitHub API
- OpenAI / Anthropic Python SDK — pluggable LLM provider
- pydantic-settings — config
- Docker + docker-compose

## Setup

```bash
cp .env.example .env
# Fill in GITHUB_WEBHOOK_SECRET, GITHUB_TOKEN
# Set LLM_PROVIDER=openai or anthropic and the corresponding API key
docker-compose up --build
```

The app runs on port `8000`. GitHub should be configured to send `pull_request` events to `https://your-host/webhook/github`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | — | HMAC secret for webhook validation |
| `GITHUB_TOKEN` | — | GitHub PAT for reading diffs and posting comments |
| `LLM_PROVIDER` | `openai` | LLM provider: `openai` or `anthropic` |
| `LLM_MODEL` | `gpt-4o-mini` | Model to use for the selected provider |
| `LLM_MAX_TOKENS` | `2048` | Max tokens per LLM response |
| `OPENAI_API_KEY` | — | Required when `LLM_PROVIDER=openai` |
| `ANTHROPIC_API_KEY` | — | Required when `LLM_PROVIDER=anthropic` |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `POST` | `/webhook/github` | GitHub PR webhook receiver |

## Suppression

Add `-- prism: ignore` to any SQL line to suppress analysis for that query:

```sql
SELECT * FROM users -- prism: ignore
```

## Adding a new reviewer

1. Extend `BaseReviewer` from `reviewers/base_reviewer.py`
2. Implement the `review()` method returning a `ReviewResult`
3. Register the reviewer in `core/analyser.py`

## Output format

```json
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
    "basis": "static|explain|runtime",
    "reason": "string",
    "estimated_improvement": ""
  },
  "explanation": "string",
  "suppressed": []
}
```
