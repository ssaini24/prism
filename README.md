# Prism

AI-powered PR reviewer — DB query optimisation module.

## What it does

Prism listens to GitHub PR webhooks, detects SQL query issues in the PR diff, and posts a structured review comment back to the PR. It is a db query reviewer.

## Data flow

```
GitHub webhook → main.py → webhook.py (HMAC verify)
                         → Analyser → diff_parser (extract SQL)
                                    → DBQueryReviewer
                                        → rules.py (5 static checks)
                                        → LLMClient → claude-haiku
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
├── .dockerignore
├── .env.example
│
├── core/
│   ├── analyser.py                # Orchestrator — routes PR diff to reviewers
│   ├── diff_parser.py             # Extracts SQL queries from unified diffs
│   └── llm_client.py              # Anthropic SDK wrapper (JSON output helper)
│
├── reviewers/
│   ├── base_reviewer.py           # Abstract base class for all reviewers
│   └── db_query/
│       ├── reviewer.py            # DB query reviewer entry point
│       ├── rules.py               # Static analysis rules
│       ├── explain_parser.py      # Phase 2: EXPLAIN JSON parser (placeholder)
│       └── prompts.py             # LLM prompt templates
│
├── github/
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

## Tech stack

- Python 3.11+
- FastAPI — webhook server
- sqlglot — SQL AST parsing
- PyGithub — GitHub API
- Anthropic Python SDK — LLM (claude-haiku-4-5)
- pydantic-settings — config
- Docker + docker-compose

## Setup

```bash
cp .env.example .env
# Fill in GITHUB_WEBHOOK_SECRET, GITHUB_TOKEN, ANTHROPIC_API_KEY
docker-compose up --build
```

The app runs on port `8000`. GitHub should be configured to send `pull_request` events to `https://your-host/webhook/github`.

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