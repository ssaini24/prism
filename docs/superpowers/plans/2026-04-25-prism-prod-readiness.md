# Prism Production Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Prism production-ready by adding label-triggered reviews, per-repo config, cost guardrails, path filtering, and removing stale webhook server code.

**Architecture:** A new `action/config_loader.py` module reads `.prism/config.yml` from the target repo at review time. All runtime behaviour (scan paths, cost threshold, extra prompt instructions, disabled rules) flows from this config. The GitHub Actions workflow gains a label gate so Prism only runs when explicitly requested. Stale FastAPI webhook code is deleted entirely.

**Tech Stack:** Python 3.11, PyYAML, GitHub Actions workflow_call, `action/orm_review.py`, `gh/commenter.py`, `models/review.py`

---

## File Map

### Created
| File | Purpose |
|---|---|
| `action/config_loader.py` | Loads & validates `.prism/config.yml`; returns a `PrismConfig` dataclass with defaults |
| `docs/prism-config-example.yml` | Reference config file repo admins copy into their repos |

### Modified
| File | What changes |
|---|---|
| `action/orm_review.py` | Use `PrismConfig` for scan_paths, cost threshold, extra_instructions, disabled_rules |
| `.github/workflows/review.yml` | Remove debug step; fix `post-comments` needs clause for optional static-analysis |
| `requirements.txt` | Add `pyyaml>=6.0`; remove `fastapi`, `uvicorn`, `pydantic-settings` (webhook-only) |
| `ssaini24/Laravel-API` → `.github/workflows/prism-review.yml` | Label trigger |
| `ssaini24/card-transactions` → `.github/workflows/prism-review.yml` | Label trigger |

### Deleted (stale webhook server only)
`main.py`, `gh/webhook.py`, `gh/auth.py`

> **Note:** `core/`, `reviewers/`, and `config/` are NOT deleted — they are all used by `action/analyze.py` → `core/analyser.py` for static analysis. `core/llm_client.py`, `core/db_explainer.py`, `core/feedback_store.py`, `core/orm_detector.py`, `core/orm_translator.py`, and all `reviewers/` subdirs are active code.

---

## Task 1: Add `pyyaml` and strip webhook-only dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Update requirements.txt**

Replace the entire file with:

```
pydantic==2.10.3
anthropic==0.40.0
openai>=1.30.0
PyGithub==2.5.0
sqlglot==25.32.0
python-dotenv==1.0.1
mcp>=1.0.0
pyyaml>=6.0
```

Removed: `fastapi`, `uvicorn[standard]`, `pydantic-settings` — only used by the webhook server, never by the Actions scripts.
Added: `pyyaml>=6.0` — needed by `config_loader.py`.

- [ ] **Step 2: Verify nothing in the Actions path imports removed packages**

```bash
grep -r "fastapi\|uvicorn\|pydantic_settings\|pydantic-settings" \
  action/ gh/commenter.py models/ --include="*.py"
```

Expected: no output (zero matches in the action path).

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: remove webhook-only deps, add pyyaml"
```

---

## Task 2: Create `action/config_loader.py`

**Files:**
- Create: `action/config_loader.py`
- Create: `docs/prism-config-example.yml`

- [ ] **Step 1: Write `action/config_loader.py`**

`scan_paths` supports glob patterns with `**` (e.g. `app/**/Models`) so repo admins don't need to list every variable package directory. `cost_threshold_usd` is intentionally absent — it is controlled by the Prism repo only via an env var in `review.yml`. `extra_instructions` is not supported.

```python
"""Loads .prism/config.yml from a target Laravel repository.

If the file is absent or malformed, all fields fall back to safe defaults.
Repo admins control behaviour by committing .prism/config.yml to their repo.

Supported keys:
  scan_paths     - list of path glob patterns (** supported); default: ["app/**", "database/migrations/"]
  disabled_rules - list of issue type strings to suppress; default: []

NOT repo-configurable (Prism repo controls these):
  cost_threshold_usd - set via COST_THRESHOLD_USD env var in review.yml
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import yaml

logger = logging.getLogger(__name__)

_DEFAULTS: dict = {
    "scan_paths": ["app/**", "database/migrations/"],
    "disabled_rules": [],
}

_VALID_KEYS = set(_DEFAULTS.keys())


def _compile_scan_pattern(pattern: str) -> re.Pattern:
    """Convert a path glob pattern (supporting ** and *) to a compiled regex.

    Examples:
        "app/**"           → matches any file under app/
        "app/**/Models"    → matches app/Business/Models/Foo.php, app/Models/Foo.php
        "database/migrations/" → matches files directly under that prefix
    """
    parts = re.split(r'(\*\*|\*)', pattern)
    regex = ''.join(
        '.*' if p == '**' else '[^/]*' if p == '*' else re.escape(p)
        for p in parts
    )
    return re.compile(r'^' + regex + r'(/|$)')


@dataclass
class PrismConfig:
    scan_paths: list[str]
    disabled_rules: list[str]

    def should_scan(self, file_path: str) -> bool:
        """Return True if file_path matches any scan_paths glob pattern."""
        return any(_compile_scan_pattern(p).match(file_path) for p in self.scan_paths)

    def is_rule_disabled(self, rule_type: str) -> bool:
        return rule_type in self.disabled_rules


def load_config(laravel_path: str | None) -> PrismConfig:
    """Load .prism/config.yml from the target repo, merging with defaults.

    Args:
        laravel_path: Absolute path to the checked-out target repo root.
                      Pass None to use defaults only.

    Returns:
        PrismConfig with all fields populated (from file or defaults).
    """
    merged = dict(_DEFAULTS)

    if laravel_path:
        config_file = os.path.join(laravel_path, ".prism", "config.yml")
        if os.path.exists(config_file):
            try:
                with open(config_file, encoding="utf-8") as fh:
                    repo_config: dict = yaml.safe_load(fh) or {}

                unknown = set(repo_config) - _VALID_KEYS
                if unknown:
                    logger.warning("[Config] Unknown keys in .prism/config.yml: %s — ignored", unknown)

                for key in _VALID_KEYS:
                    if key in repo_config and repo_config[key] is not None:
                        merged[key] = repo_config[key]

                logger.info("[Config] Loaded .prism/config.yml — scan_paths=%s", merged["scan_paths"])
            except yaml.YAMLError as exc:
                logger.warning("[Config] Malformed .prism/config.yml: %s — using defaults", exc)
            except OSError as exc:
                logger.warning("[Config] Cannot read .prism/config.yml: %s — using defaults", exc)
        else:
            logger.info("[Config] No .prism/config.yml found — using defaults")

    return PrismConfig(
        scan_paths=merged["scan_paths"],
        disabled_rules=list(merged["disabled_rules"]),
    )
```

- [ ] **Step 2: Write `docs/prism-config-example.yml`**

```yaml
# .prism/config.yml — Prism reviewer configuration
# Copy this file to .prism/config.yml in your repository.
# All fields are optional. Omit any field to use the Prism default.

# Paths to scan in PR diffs. Supports glob patterns:
#   *  matches any path segment (no slashes)
#   ** matches any number of path segments (including zero)
#
# Default: ["app/**", "database/migrations/"]
#
# Examples:
#   app/**                  → all PHP files anywhere under app/
#   app/**/Models           → app/Models/, app/Business/Models/, app/Payments/Models/, etc.
#   app/Http/Controllers    → only the Controllers directory
scan_paths:
  - app/**/Repositories
  - app/**/Services
  - app/Http/Controllers
  - database/migrations

# Issue types to suppress. Matching issues are dropped before posting comments.
# Valid values: select_star, missing_where_clause, function_on_indexed_column,
#   join_without_condition, n_plus_one_pattern, destructive_ddl, unsafe_alter_table,
#   full_table_scan, missing_index, inefficient_subquery, implicit_type_conversion,
#   unbounded_result_set
# Default: [] (all issues reported)
disabled_rules: []
```

- [ ] **Step 3: Commit**

```bash
git add action/config_loader.py docs/prism-config-example.yml
git commit -m "feat(config): add PrismConfig loader from .prism/config.yml"
```

---

## Task 3: Wire `PrismConfig` into `orm_review.py`

**Files:**
- Modify: `action/orm_review.py`

This task has four integration points: (A) path filtering, (B) cost threshold, (C) extra instructions in system prompt, (D) disabled rules filtering.

- [ ] **Step 1: Import `load_config` at the top of `orm_review.py`**

Add after the existing imports:

```python
from action.config_loader import load_config, PrismConfig  # noqa: E402
```

Because `orm_review.py` is invoked as `python prism-action/action/orm_review.py`, add the project root to `sys.path` first. At the very top of the file after `from __future__ import annotations`:

```python
import sys
import os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
```

- [ ] **Step 2: Add a helper to compute current running cost**

Add after `_log_cost_summary()`:

```python
def _current_cost_usd(model: str) -> float:
    cost_in, cost_out = _COST_PER_1K.get(model, (0.0, 0.0))
    return (_usage["input_tokens"] / 1000 * cost_in) + (_usage["output_tokens"] / 1000 * cost_out)
```

- [ ] **Step 3: Add path filtering to `extract_php_blocks()`**

Change the function signature to accept `prism_config`:

```python
def extract_php_blocks(diff: str, prism_config: PrismConfig | None = None) -> list[dict]:
    """
    Parse a unified diff and return per-method blocks for PHP files.
    Only files matching prism_config.should_scan() are included.
    prism_config=None means include all PHP files (backward-compatible default).
    """
    files: dict[str, dict] = {}
    current_file: str | None = None
    hunk_new_line: int = 0

    for raw_line in diff.splitlines():
        file_match = re.match(r'^\+\+\+\s+b/(.+)$', raw_line)
        if file_match:
            path = file_match.group(1)
            if not path.endswith('.php'):
                current_file = None
                hunk_new_line = 0
                continue
            if prism_config and not prism_config.should_scan(path):
                logger.debug("[ORM] Skipping %s (not in scan_paths)", path)
                current_file = None
                hunk_new_line = 0
                continue
            current_file = path
            hunk_new_line = 0
            continue
        # ... rest of the existing loop body unchanged ...
```

(Keep the rest of the function body identical to what it is now.)

- [ ] **Step 4: Add cost threshold guard to `review_blocks()`**

`cost_threshold_usd` is read from the `COST_THRESHOLD_USD` env var (set in `review.yml`), not from `PrismConfig`. Change the signature:

```python
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
```

- [ ] **Step 5: Update `main()` to load config and wire everything together**

Replace the `main()` function body from `blocks = extract_php_blocks(diff_text)` onward:

```python
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
        api_key, model, args.output, db_env, prism_config, cost_threshold_usd,
    )
    _log_cost_summary(model)
```

- [ ] **Step 6: Add `COST_THRESHOLD_USD` env var to `review.yml`**

In `.github/workflows/review.yml`, add `COST_THRESHOLD_USD` to the "Run ORM reviewer" step env block:

```yaml
      - name: Run ORM reviewer
        run: |
          python prism-action/action/orm_review.py \
            --diff=/tmp/pr.diff \
            --output=/tmp/orm-results.json \
            --laravel-path=${{ github.workspace }}/target-repo
        env:
          LLM_PROVIDER: ${{ inputs.llm_provider }}
          LLM_MODEL: claude-sonnet-4-6
          COST_THRESHOLD_USD: "1.00"
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          APP_ENV: local
          APP_KEY: base64:dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleXQ=
          DB_CONNECTION: mysql
          DB_HOST: 127.0.0.1
          DB_PORT: 3306
          DB_DATABASE: testing
          DB_USERNAME: root
          DB_PASSWORD: secret
```

- [ ] **Step 7: Commit**

```bash
git add action/orm_review.py .github/workflows/review.yml
git commit -m "feat(orm): wire PrismConfig — path filter, cost threshold (env), disabled rules"
```

---

## Task 4: Remove stale webhook server code and clean up workflow

**Files:**
- Delete: `main.py`
- Delete: `gh/webhook.py`
- Delete: `gh/auth.py`
- Modify: `.github/workflows/review.yml`

Everything in `core/`, `reviewers/`, and `config/` is kept — they are all on the active
`action/analyze.py` → `core/analyser.py` static analysis path. The `llm_provider` and
`enable_static_analysis` inputs in each repo's `prism-review.yml` control which code
paths run at runtime.

- [ ] **Step 1: Verify webhook files are not imported by any action script**

```bash
grep -r "from gh.webhook\|from gh.auth\|import main" \
  action/ gh/commenter.py models/ --include="*.py"
```

Expected: no output.

- [ ] **Step 2: Delete stale webhook server files**

```bash
rm main.py gh/webhook.py gh/auth.py
```

- [ ] **Step 3: Fix `post-comments` job `needs` clause**

`post-comments` currently has `needs: [orm-analysis, static-analysis]`. When `static-analysis` is skipped (label not set, or `enable_static_analysis: false`), this causes `post-comments` to be skipped too. Fix:

```yaml
  post-comments:
    name: Post Review Comments
    runs-on: ubuntu-latest
    needs: [orm-analysis]
    if: always() && needs.orm-analysis.result != 'skipped'
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: remove stale webhook server files (main.py, webhook.py, auth.py)"
```

---

## Task 5: Label-triggered workflow in target repos

**Files:**
- Modify: `ssaini24/Laravel-API` → `.github/workflows/prism-review.yml`
- Modify: `ssaini24/card-transactions` → `.github/workflows/prism-review.yml`

The reusable `review.yml` in Prism does not change — the label gate lives in the calling workflow so each repo controls its own trigger.

- [ ] **Step 1: Update Laravel-API workflow**

Replace `/Users/sahil/ai-projects/Laravel-API/.github/workflows/prism-review.yml` with:

```yaml
name: Prism Code Review

on:
  pull_request:
    types: [labeled]

jobs:
  review:
    if: github.event.label.name == 'prism-review'
    uses: ssaini24/prism/.github/workflows/review.yml@main
    with:
      llm_provider: "claude-code"
      enable_static_analysis: false
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      CLAUDE_CREDENTIALS: ${{ secrets.CLAUDE_CREDENTIALS }}
```

- [ ] **Step 2: Commit and push Laravel-API**

```bash
cd /Users/sahil/ai-projects/Laravel-API
git add .github/workflows/prism-review.yml
git -c commit.gpgsign=false commit -m "ci: trigger Prism only on prism-review label"
git push origin main
```

- [ ] **Step 3: Update card-transactions workflow**

Replace `/Users/sahil/ai-projects/card-transactions/.github/workflows/prism-review.yml` with:

```yaml
name: Prism Code Review

on:
  pull_request:
    types: [labeled]

jobs:
  review:
    if: github.event.label.name == 'prism-review'
    uses: ssaini24/prism/.github/workflows/review.yml@main
    with:
      llm_provider: "claude-code"
      enable_static_analysis: false
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      CLAUDE_CREDENTIALS: ${{ secrets.CLAUDE_CREDENTIALS }}
```

- [ ] **Step 4: Commit and push card-transactions**

```bash
cd /Users/sahil/ai-projects/card-transactions
git add .github/workflows/prism-review.yml
git -c commit.gpgsign=false commit -m "ci: trigger Prism only on prism-review label"
git push origin main
```

- [ ] **Step 5: Create the `prism-review` label in both repos**

```bash
gh label create "prism-review" --color "8A2BE2" --description "Run Prism ORM reviewer" \
  --repo ssaini24/Laravel-API

gh label create "prism-review" --color "8A2BE2" --description "Run Prism ORM reviewer" \
  --repo ssaini24/card-transactions
```

Expected output: `✓ Label "prism-review" created`

- [ ] **Step 6: Commit and push Prism**

```bash
cd /Users/sahil/ai-projects/prism
git add .github/workflows/review.yml
git -c commit.gpgsign=false commit -m "fix(workflow): remove static-analysis from post-comments needs"
git push origin main
```

---

## Task 6: Add `.prism/config.yml` to target repos

**Files:**
- Create: `ssaini24/Laravel-API` → `.prism/config.yml`
- Create: `ssaini24/card-transactions` → `.prism/config.yml`

- [ ] **Step 1: Create `.prism/config.yml` for Laravel-API**

```bash
mkdir -p /Users/sahil/ai-projects/Laravel-API/.prism
```

Write `/Users/sahil/ai-projects/Laravel-API/.prism/config.yml`:

```yaml
scan_paths:
  - app/Http/Controllers
  - app/**/Models
  - database/migrations

disabled_rules: []
```

- [ ] **Step 2: Commit and push Laravel-API config**

```bash
cd /Users/sahil/ai-projects/Laravel-API
git add .prism/config.yml
git -c commit.gpgsign=false commit -m "chore: add .prism/config.yml for Prism reviewer"
git push origin main
```

- [ ] **Step 3: Create `.prism/config.yml` for card-transactions**

```bash
mkdir -p /Users/sahil/ai-projects/card-transactions/.prism
```

Write `/Users/sahil/ai-projects/card-transactions/.prism/config.yml`:

```yaml
scan_paths:
  - app/**/Repositories
  - app/**/Services
  - app/Http/Controllers
  - database/migrations

disabled_rules: []
```

- [ ] **Step 4: Commit and push card-transactions config**

```bash
cd /Users/sahil/ai-projects/card-transactions
git add .prism/config.yml
git -c commit.gpgsign=false commit -m "chore: add .prism/config.yml for Prism reviewer"
git push origin main
```

---

## How to test after all tasks are done

1. Open a PR on `ssaini24/Laravel-API` or `ssaini24/card-transactions`
2. Prism workflow does **not** trigger automatically
3. Add the `prism-review` label to the PR
4. Prism workflow triggers; ORM analysis runs
5. Check Actions log for:
   - `[Config] Loaded .prism/config.yml` line
   - `[ORM] Extracted N PHP block(s) from diff (scan_paths=[...])`
   - `[ORM] Cost threshold: $1.00` (from `COST_THRESHOLD_USD` env var in `review.yml`)
   - Cost summary at end showing tokens + estimated cost
6. Review comments appear only on files matching `scan_paths` globs
7. To test cost threshold: temporarily set `COST_THRESHOLD_USD: "0.001"` in `review.yml` and verify the log shows the threshold warning and stops early (revert after testing)
