# ORM Context-Aware Analysis — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reusable GitHub Actions workflow to Prism that gives the LLM live database schema access (via Laravel Boost MCP) when reviewing Eloquent ORM code in PHP PRs.

**Architecture:** Three parallel jobs — `orm-analysis` (PHP + Boost MCP + real MySQL) and `static-analysis` (existing Python Prism) run in parallel; `post-comments` waits for both, merges results, and posts all PR comments via the existing Python commenter. Target repos add a single 5-line caller workflow.

**Tech Stack:** Python 3.11, PHP 8.2, `echolabs/prism` (PHP LLM client), `laravel/boost` (MCP schema server), GitHub Actions reusable workflows, pytest, MySQL 8.0 service containers.

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `action/analyze.py` | **Create** | Python CLI: parse diff → run Analyser → write results JSON |
| `action/post_comments.py` | **Create** | Load both artifacts → call PRCommenter → post PR comments |
| `action/composer.json` | **Create** | PHP deps for ORM reviewer (prism, boost) |
| `action/entrypoint.sh` | **Create** | Install PHP deps, verify Boost, run review.php |
| `action/review.php` | **Create** | PHP ORM reviewer: extract PHP blocks → Prism + Boost loop → write JSON |
| `.github/workflows/review.yml` | **Create** | Reusable 3-job workflow definition |
| `action/caller-template.yml` | **Create** | Copy-paste template for target repos |
| `tests/__init__.py` | **Create** | Makes tests/ a package |
| `tests/action/__init__.py` | **Create** | Makes tests/action/ a package |
| `tests/action/test_analyze.py` | **Create** | Tests for action/analyze.py |
| `tests/action/test_post_comments.py` | **Create** | Tests for action/post_comments.py |
| `Dockerfile` | **No change** | Spec mentioned Docker for Job 2, but the plan runs Python directly (no Docker) to avoid requiring a published GHCR image as a prerequisite. Functionally equivalent — easier to iterate. |

---

## Task 0: Verify Third-Party Package APIs

> **This task must be completed before writing any PHP code.** The plan uses best-guess API based on MCP spec and known prism-php patterns. Verify and adjust Tasks 4–5 accordingly.

**Files:** No code changes — research only.

- [ ] **Step 1: Check laravel/boost MCP tool names**

  Run in a Laravel app with `laravel/boost` installed:
  ```bash
  composer require laravel/boost --dev
  php artisan boost:mcp --list-tools
  ```
  Confirm the exact names for: list tables, describe table schema, get table indexes.
  Update `action/review.php` system prompt and tool references to match.

- [ ] **Step 2: Check prism-php relay MCP transport**

  Review docs at `https://prismphp.com` and `https://github.com/echolabs/prism`.
  Confirm answers to:
  - Is the package `echolabs/prism` or `prism-php/prism`? Update `action/composer.json`.
  - Does `prism-php/relay` exist as a separate package, or is MCP client built into prism?
  - Does it support stdio MCP transport (subprocess), or HTTP only?

- [ ] **Step 3: Record findings**

  Write a comment block at the top of `action/review.php` documenting the verified package names and tool names found in Steps 1–2. This replaces the placeholders in Task 4.

---

## Task 1: Python Static Analysis CLI Script

**Files:**
- Create: `action/analyze.py`
- Create: `tests/__init__.py`
- Create: `tests/action/__init__.py`
- Create: `tests/action/test_analyze.py`

- [ ] **Step 1: Write failing tests**

  Create `tests/__init__.py` (empty) and `tests/action/__init__.py` (empty), then create `tests/action/test_analyze.py`:

  ```python
  """Tests for action/analyze.py"""
  from __future__ import annotations

  import json
  import sys
  import os
  from unittest.mock import MagicMock, patch

  import pytest

  sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

  SAMPLE_SQL_DIFF = """\
  diff --git a/queries.sql b/queries.sql
  +++ b/queries.sql
  @@ -1,3 +1,5 @@
  +SELECT * FROM users;
  """

  SAMPLE_PHP_DIFF = """\
  diff --git a/app/Http/Controllers/UserController.php b/app/Http/Controllers/UserController.php
  +++ b/app/Http/Controllers/UserController.php
  @@ -1,3 +1,6 @@
  +    public function index() {
  +        $users = User::all();
  +        return response()->json($users);
  +    }
  """


  def test_analyze_writes_empty_list_when_no_results(tmp_path):
      diff_file = tmp_path / "test.diff"
      diff_file.write_text(SAMPLE_SQL_DIFF)
      output_file = tmp_path / "results.json"

      with patch("action.analyze.Analyser") as MockAnalyser:
          mock_instance = MagicMock()
          mock_instance.analyse_pr.return_value = []
          MockAnalyser.return_value = mock_instance

          from action.analyze import run
          run(str(diff_file), str(output_file), repo="")

      assert output_file.exists()
      data = json.loads(output_file.read_text())
      assert data == []


  def test_analyze_serializes_results_correctly(tmp_path):
      from models.review import CostAnalysis, ExtractedQuery, Issue, ReviewResult

      diff_file = tmp_path / "test.diff"
      diff_file.write_text(SAMPLE_SQL_DIFF)
      output_file = tmp_path / "results.json"

      query = ExtractedQuery(raw="SELECT * FROM users", file="queries.sql", line=1)
      result = ReviewResult(
          issues=[Issue(
              type="select_star", severity="medium", confidence="high",
              line=1, description="Avoid SELECT *", suggestion="Specify columns"
          )],
          explanation="Select star detected.",
      )

      with patch("action.analyze.Analyser") as MockAnalyser:
          mock_instance = MagicMock()
          mock_instance.analyse_pr.return_value = [(query, result)]
          MockAnalyser.return_value = mock_instance

          from action.analyze import run
          run(str(diff_file), str(output_file), repo="")

      data = json.loads(output_file.read_text())
      assert len(data) == 1
      assert data[0]["query"]["raw"] == "SELECT * FROM users"
      assert data[0]["query"]["file"] == "queries.sql"
      assert data[0]["result"]["issues"][0]["type"] == "select_star"


  def test_analyze_passes_diff_text_to_analyser(tmp_path):
      diff_file = tmp_path / "test.diff"
      diff_file.write_text(SAMPLE_SQL_DIFF)
      output_file = tmp_path / "results.json"

      with patch("action.analyze.Analyser") as MockAnalyser:
          mock_instance = MagicMock()
          mock_instance.analyse_pr.return_value = []
          MockAnalyser.return_value = mock_instance

          from action.analyze import run
          run(str(diff_file), str(output_file), repo="owner/repo")

          mock_instance.analyse_pr.assert_called_once_with(SAMPLE_SQL_DIFF, repo="owner/repo")
  ```

- [ ] **Step 2: Run tests to confirm they fail**

  ```bash
  cd /Users/sahil/ai-projects/prism
  source .venv/bin/activate
  pytest tests/action/test_analyze.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'action.analyze'`

- [ ] **Step 3: Implement `action/analyze.py`**

  Create `action/__init__.py` (empty), then create `action/analyze.py`:

  ```python
  """CLI entry point for running Prism static analysis on a diff file.

  Usage:
      python action/analyze.py --diff <path> --output <path> [--repo owner/repo]

  Environment variables:
      LLM_PROVIDER, ANTHROPIC_API_KEY (or OPENAI_API_KEY), ENABLE_ORM_REVIEW=false
  """
  from __future__ import annotations

  import argparse
  import json
  import os
  import sys

  sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

  from core.analyser import Analyser


  def run(diff_path: str, output_path: str, repo: str) -> None:
      with open(diff_path) as f:
          diff_text = f.read()

      analyser = Analyser()
      results = analyser.analyse_pr(diff_text, repo=repo)

      output = [
          {"query": query.model_dump(), "result": result.model_dump()}
          for query, result in results
      ]

      with open(output_path, "w") as f:
          json.dump(output, f, indent=2)

      print(f"[analyze] Wrote {len(output)} results to {output_path}")


  def main() -> None:
      parser = argparse.ArgumentParser(description="Run Prism static analysis on a diff")
      parser.add_argument("--diff", required=True, help="Path to unified diff file")
      parser.add_argument("--output", required=True, help="Path to write results JSON")
      parser.add_argument("--repo", default="", help="Repo full name e.g. owner/repo")
      args = parser.parse_args()
      run(args.diff, args.output, args.repo)


  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 4: Run tests to confirm they pass**

  ```bash
  pytest tests/action/test_analyze.py -v
  ```
  Expected:
  ```
  tests/action/test_analyze.py::test_analyze_writes_empty_list_when_no_results PASSED
  tests/action/test_analyze.py::test_analyze_serializes_results_correctly PASSED
  tests/action/test_analyze.py::test_analyze_passes_diff_text_to_analyser PASSED
  ```

- [ ] **Step 5: Smoke test with a real diff**

  ```bash
  echo '+SELECT * FROM users;' > /tmp/test.diff
  LLM_PROVIDER=claude-code python action/analyze.py \
    --diff=/tmp/test.diff --output=/tmp/test-results.json --repo=test/repo
  cat /tmp/test-results.json
  ```
  Expected: JSON array with at least one result containing `select_star` issue.

- [ ] **Step 6: Commit**

  ```bash
  git add action/__init__.py action/analyze.py tests/__init__.py tests/action/__init__.py tests/action/test_analyze.py
  git commit -m "feat: add CLI entry point for static analysis (action/analyze.py)"
  ```

---

## Task 2: Post Comments Script

**Files:**
- Create: `action/post_comments.py`
- Create: `tests/action/test_post_comments.py`

- [ ] **Step 1: Write failing tests**

  Create `tests/action/test_post_comments.py`:

  ```python
  """Tests for action/post_comments.py"""
  from __future__ import annotations

  import json
  import os
  import sys
  from unittest.mock import MagicMock, patch

  import pytest

  sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


  SAMPLE_ARTIFACT = [
      {
          "query": {
              "raw": "SELECT * FROM users",
              "file": "queries.sql",
              "line": 1,
              "suppressed": False,
          },
          "result": {
              "issues": [
                  {
                      "type": "select_star",
                      "severity": "medium",
                      "confidence": "high",
                      "line": 1,
                      "description": "Avoid SELECT *",
                      "suggestion": "Specify columns explicitly",
                  }
              ],
              "optimized_query": "SELECT id, name FROM users",
              "index_suggestions": [],
              "migration_warnings": [],
              "cost_analysis": {
                  "level": "medium",
                  "basis": "static",
                  "reason": "select_star detected",
                  "estimated_improvement": "",
              },
              "explanation": "SELECT * detected.",
              "suppressed": [],
          },
      }
  ]


  def test_load_artifact_returns_empty_for_missing_file():
      from action.post_comments import load_artifact
      result = load_artifact("/nonexistent/path.json")
      assert result == []


  def test_load_artifact_deserializes_correctly(tmp_path):
      from action.post_comments import load_artifact
      from models.review import ExtractedQuery, ReviewResult

      artifact_file = tmp_path / "results.json"
      artifact_file.write_text(json.dumps(SAMPLE_ARTIFACT))

      pairs = load_artifact(str(artifact_file))

      assert len(pairs) == 1
      query, result = pairs[0]
      assert isinstance(query, ExtractedQuery)
      assert isinstance(result, ReviewResult)
      assert query.raw == "SELECT * FROM users"
      assert result.issues[0].type == "select_star"


  def test_post_comments_calls_commenter_with_merged_results(tmp_path):
      from action.post_comments import post

      orm_file = tmp_path / "orm.json"
      static_file = tmp_path / "static.json"
      orm_file.write_text(json.dumps(SAMPLE_ARTIFACT))
      static_file.write_text(json.dumps(SAMPLE_ARTIFACT))

      with patch("action.post_comments.PRCommenter") as MockCommenter:
          mock_instance = MagicMock()
          MockCommenter.return_value = mock_instance

          post(
              orm_results=str(orm_file),
              static_results=str(static_file),
              owner="testowner",
              repo="testrepo",
              pr_number=42,
              sha="abc123",
          )

          mock_instance.post_review.assert_called_once()
          call_kwargs = mock_instance.post_review.call_args
          assert call_kwargs.kwargs["owner"] == "testowner"
          assert call_kwargs.kwargs["repo_name"] == "testrepo"
          assert call_kwargs.kwargs["pr_number"] == 42
          assert len(call_kwargs.kwargs["results"]) == 2  # one from each artifact


  def test_post_comments_works_with_one_missing_artifact(tmp_path):
      from action.post_comments import post

      static_file = tmp_path / "static.json"
      static_file.write_text(json.dumps(SAMPLE_ARTIFACT))

      with patch("action.post_comments.PRCommenter") as MockCommenter:
          mock_instance = MagicMock()
          MockCommenter.return_value = mock_instance

          post(
              orm_results="/nonexistent/orm.json",
              static_results=str(static_file),
              owner="testowner",
              repo="testrepo",
              pr_number=1,
              sha="abc123",
          )

          call_kwargs = mock_instance.post_review.call_args
          assert len(call_kwargs.kwargs["results"]) == 1
  ```

- [ ] **Step 2: Run tests to confirm they fail**

  ```bash
  pytest tests/action/test_post_comments.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'action.post_comments'`

- [ ] **Step 3: Implement `action/post_comments.py`**

  ```python
  """Merges ORM and static analysis artifacts and posts PR comments.

  Usage:
      python action/post_comments.py \
          --orm-results /tmp/orm-results.json \
          --static-results /tmp/static-results.json \
          --owner <owner> --repo <repo> --pr <number> --sha <sha>

  Environment variables:
      GITHUB_TOKEN — required for posting comments
  """
  from __future__ import annotations

  import argparse
  import json
  import os
  import sys

  sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

  from gh.commenter import PRCommenter
  from models.review import CostAnalysis, ExtractedQuery, Issue, ReviewResult


  def load_artifact(path: str) -> list[tuple[ExtractedQuery, ReviewResult]]:
      """Load a results JSON artifact. Returns [] if file is missing or malformed."""
      if not path or not os.path.exists(path):
          return []
      try:
          with open(path) as f:
              data = json.load(f)
      except (json.JSONDecodeError, OSError) as exc:
          print(f"[post_comments] Warning: could not load {path}: {exc}", file=sys.stderr)
          return []

      pairs = []
      for item in data:
          try:
              query = ExtractedQuery(**item["query"])
              rd = item["result"]
              cost_raw = rd.get("cost_analysis", {})
              result = ReviewResult(
                  issues=[Issue(**i) for i in rd.get("issues", [])],
                  optimized_query=rd.get("optimized_query", ""),
                  index_suggestions=rd.get("index_suggestions", []),
                  migration_warnings=rd.get("migration_warnings", []),
                  cost_analysis=CostAnalysis(
                      level=cost_raw.get("level", "low"),
                      basis=cost_raw.get("basis", "static"),
                      reason=cost_raw.get("reason", ""),
                      estimated_improvement=cost_raw.get("estimated_improvement", ""),
                  ),
                  explanation=rd.get("explanation", ""),
                  suppressed=rd.get("suppressed", []),
              )
              pairs.append((query, result))
          except Exception as exc:
              print(f"[post_comments] Warning: skipping malformed entry: {exc}", file=sys.stderr)
      return pairs


  def post(
      orm_results: str,
      static_results: str,
      owner: str,
      repo: str,
      pr_number: int,
      sha: str,
  ) -> None:
      results = load_artifact(orm_results) + load_artifact(static_results)
      print(f"[post_comments] Posting review: {len(results)} blocks total.")
      commenter = PRCommenter()
      commenter.post_review(
          owner=owner,
          repo_name=repo,
          pr_number=pr_number,
          results=results,
          commit_sha=sha,
      )


  def main() -> None:
      parser = argparse.ArgumentParser()
      parser.add_argument("--orm-results", default="")
      parser.add_argument("--static-results", default="")
      parser.add_argument("--owner", required=True)
      parser.add_argument("--repo", required=True)
      parser.add_argument("--pr", required=True, type=int)
      parser.add_argument("--sha", required=True)
      args = parser.parse_args()
      post(
          orm_results=args.orm_results,
          static_results=args.static_results,
          owner=args.owner,
          repo=args.repo,
          pr_number=args.pr,
          sha=args.sha,
      )


  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 4: Run tests to confirm they pass**

  ```bash
  pytest tests/action/test_post_comments.py -v
  ```
  Expected:
  ```
  tests/action/test_post_comments.py::test_load_artifact_returns_empty_for_missing_file PASSED
  tests/action/test_post_comments.py::test_load_artifact_deserializes_correctly PASSED
  tests/action/test_post_comments.py::test_post_comments_calls_commenter_with_merged_results PASSED
  tests/action/test_post_comments.py::test_post_comments_works_with_one_missing_artifact PASSED
  ```

- [ ] **Step 5: Run all tests**

  ```bash
  pytest tests/ -v
  ```
  Expected: All tests pass, no failures.

- [ ] **Step 6: Commit**

  ```bash
  git add action/post_comments.py tests/action/test_post_comments.py
  git commit -m "feat: add post_comments script to merge artifacts and post PR review"
  ```

---

## Task 3: PHP Action Setup

> **Prerequisite:** Complete Task 0 first — package names in `composer.json` depend on verified findings.

**Files:**
- Create: `action/composer.json`
- Create: `action/entrypoint.sh`

- [ ] **Step 1: Create `action/composer.json`**

  Replace `echolabs/prism` and `echolabs/prism-relay` with the verified package names from Task 0.

  ```json
  {
      "name": "ssaini24/prism-action",
      "description": "PHP ORM reviewer for Prism GitHub Action",
      "require": {
          "php": "^8.2",
          "echolabs/prism": "^0.36",
          "echolabs/prism-relay": "^0.1"
      },
      "config": {
          "optimize-autoloader": true,
          "preferred-install": "dist"
      },
      "minimum-stability": "dev",
      "prefer-stable": true
  }
  ```

- [ ] **Step 2: Create `action/entrypoint.sh`**

  ```bash
  #!/usr/bin/env bash
  set -euo pipefail

  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  echo "[entrypoint] Installing PHP dependencies..."
  composer install --no-interaction --prefer-dist --working-dir="$SCRIPT_DIR"

  echo "[entrypoint] Verifying Boost is available in target repo..."
  if ! php "${LARAVEL_APP_PATH}/artisan" boost:mcp --help &>/dev/null; then
    echo "[entrypoint] WARNING: laravel/boost not found. ORM analysis will run without schema tools."
    echo "[entrypoint] Install with: composer require laravel/boost --dev"
  fi

  echo "[entrypoint] Starting ORM review..."
  php "$SCRIPT_DIR/review.php" "$@"
  ```

- [ ] **Step 3: Make entrypoint executable and test it locally**

  ```bash
  chmod +x action/entrypoint.sh
  bash action/entrypoint.sh --help
  ```
  Expected: Composer install output, then `review.php` usage message (or "file not found" until Task 4).

- [ ] **Step 4: Commit**

  ```bash
  git add action/composer.json action/entrypoint.sh
  git commit -m "feat: add PHP action setup (composer.json + entrypoint.sh)"
  ```

---

## Task 4: PHP ORM Reviewer Script

> **Prerequisite:** Complete Task 0. The Boost tool names and Prism PHP API used below are placeholders — replace with verified values before running.

**Files:**
- Create: `action/review.php`

- [ ] **Step 1: Create `action/review.php`**

  ```php
  <?php
  /**
   * ORM Reviewer — analyzes PHP Eloquent code from a PR diff using
   * Prism PHP (LLM client) + Laravel Boost (MCP schema server).
   *
   * Usage:
   *   php action/review.php --diff=/tmp/pr.diff --output=/tmp/orm-results.json
   *
   * Environment variables:
   *   ANTHROPIC_API_KEY     — LLM API key
   *   LARAVEL_APP_PATH      — absolute path to the target Laravel app
   *   DB_CONNECTION, DB_HOST, DB_PORT, DB_DATABASE, DB_USERNAME, DB_PASSWORD
   *
   * NOTE: Boost tool names (list-tables, describe-table, table-indexes) must
   * be verified against laravel/boost docs (see Task 0) and updated here.
   */

  declare(strict_types=1);

  require_once __DIR__ . '/vendor/autoload.php';

  use EchoLabs\Prism\Facades\Prism;
  use EchoLabs\Prism\Enums\Provider;

  // ── System prompt ──────────────────────────────────────────────────────────

  const SYSTEM_PROMPT = <<<'PROMPT'
  # Role: Senior Laravel ORM Specialist & Database Auditor
  You are an expert Laravel developer reviewing PR diffs. Your specialty is
  Eloquent ORM performance, database integrity, and modern Laravel best practices.

  # Contextual Awareness
  You have access to live database schema through Laravel Boost MCP tools.
  Before flagging any issue involving a table name, column, or index — you MUST
  call the relevant tool to verify against the actual migrated schema.
  Available tools: list-tables, describe-table, table-indexes.

  # Objectives
  1. Schema Validation: If you see a table name in ORM code, call describe-table.
     If the table is missing or renamed, flag it as severity: high.
  2. N+1 Detection: Identify loops where relationships are accessed without
     eager loading (missing with()). Always suggest the corrected with() call.
  3. Column Selection: Flag select() or pluck() opportunities for large datasets.
  4. Performance: Suggest chunk(), lazy(), or cursor() over get() where appropriate.
  5. Modern Standards: Flag non-idiomatic Laravel patterns.

  # Rules
  - Never assume a table or column exists — call a tool to verify.
  - Never hallucinate that a migration exists because it is mentioned in a comment.
  - Set confidence: "low" if you could not verify schema due to tool failure.
  - Set cost_analysis.basis to "explain" when findings are backed by schema/index
    data from tools; "static" otherwise.

  # Output Format
  Respond ONLY with a valid JSON array. No markdown, no prose.
  Each element represents one reviewed PHP code block:

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
          "description": "Relationship orders loaded inside loop without eager loading.",
          "suggestion": "Add ->with('orders') before iterating."
        }
      ],
      "optimized_query": "User::with('orders')->where('active', true)->get()",
      "index_suggestions": ["CREATE INDEX idx_orders_user_id ON orders(user_id);"],
      "migration_warnings": [],
      "cost_analysis": {
        "level": "high",
        "basis": "explain",
        "reason": "No index on orders.user_id — full scan per loop iteration.",
        "estimated_improvement": "~99% row reduction with index"
      },
      "explanation": "2-3 sentence summary of findings for this block."
    }
  ]

  Valid issue types: select_star, missing_where_clause, function_on_indexed_column,
  join_without_condition, n_plus_one_pattern, destructive_ddl, unsafe_alter_table,
  full_table_scan, missing_index, inefficient_subquery, implicit_type_conversion,
  unbounded_result_set

  If no issues are found for a block, return an empty array [].
  PROMPT;

  // ── Diff parser ────────────────────────────────────────────────────────────

  /**
   * Extract PHP file blocks from a unified diff.
   * Returns array of ['file' => string, 'line' => int, 'raw' => string].
   */
  function extractPhpBlocks(string $diff): array
  {
      $blocks   = [];
      $file     = null;
      $line     = 0;
      $rawLines = [];
      $blockStart = 0;

      foreach (explode("\n", $diff) as $rawLine) {
          if (str_starts_with($rawLine, '+++ b/')) {
              if ($file !== null && !empty($rawLines) && str_ends_with($file, '.php')) {
                  $blocks[] = ['file' => $file, 'line' => $blockStart, 'raw' => implode("\n", $rawLines)];
              }
              $file     = substr($rawLine, 6);
              $line     = 0;
              $rawLines = [];
              $blockStart = 0;
              continue;
          }

          if (str_starts_with($rawLine, '@@')) {
              if (preg_match('/\+(\d+)/', $rawLine, $m)) {
                  $line = (int)$m[1] - 1;
              }
              continue;
          }

          if (str_starts_with($rawLine, '+') && !str_starts_with($rawLine, '+++')) {
              $line++;
              if ($blockStart === 0) {
                  $blockStart = $line;
              }
              $rawLines[] = substr($rawLine, 1);
          } elseif (!str_starts_with($rawLine, '-')) {
              $line++;
          }
      }

      // Flush last block
      if ($file !== null && !empty($rawLines) && str_ends_with($file, '.php')) {
          $blocks[] = ['file' => $file, 'line' => $blockStart, 'raw' => implode("\n", $rawLines)];
      }

      return $blocks;
  }

  // ── Boost MCP client ───────────────────────────────────────────────────────

  /**
   * Start the Boost MCP subprocess and return a handle.
   * Returns null if Boost is not available (graceful degradation).
   *
   * NOTE: Verify the exact command with Task 0 findings.
   * The Relay API below is illustrative — update to match prism-php/relay docs.
   */
  function startBoostMcp(): mixed
  {
      $appPath = getenv('LARAVEL_APP_PATH') ?: getcwd();
      $artisan = $appPath . '/artisan';

      if (!file_exists($artisan)) {
          fwrite(STDERR, "[review] WARNING: artisan not found at {$artisan}. Running without schema tools.\n");
          return null;
      }

      try {
          // TODO: Replace with verified Relay API from Task 0
          // e.g. return \EchoLabs\PrismRelay\McpClient::stdio("php {$artisan} boost:mcp");
          return null; // placeholder until Task 0 verified
      } catch (\Throwable $e) {
          fwrite(STDERR, "[review] WARNING: Boost MCP failed to start: {$e->getMessage()}\n");
          return null;
      }
  }

  // ── LLM review ─────────────────────────────────────────────────────────────

  function reviewBlock(array $block, mixed $boostMcp): ?array
  {
      $userPrompt = "Review this PHP/Eloquent code from file `{$block['file']}` (line {$block['line']}):\n\n"
          . "```php\n{$block['raw']}\n```";

      try {
          $request = Prism::text()
              ->using(Provider::Anthropic, 'claude-haiku-4-5-20251001')
              ->withSystemPrompt(SYSTEM_PROMPT)
              ->withPrompt($userPrompt)
              ->withMaxSteps(10);

          // Attach Boost tools if available
          // TODO: Replace with verified Relay API from Task 0
          // if ($boostMcp !== null) {
          //     $request = $request->withMcpServer($boostMcp);
          // }

          $response = $request->generate();
          $text = $response->text;

          // Strip markdown fences if present
          $text = preg_replace('/^```(?:json)?\s*/m', '', $text);
          $text = preg_replace('/\s*```$/m', '', $text);
          $text = trim($text);

          $parsed = json_decode($text, true);
          if (!is_array($parsed)) {
              fwrite(STDERR, "[review] WARNING: LLM returned non-JSON for {$block['file']}:{$block['line']}\n");
              return null;
          }

          // Inject file/line into each result element
          return array_map(function (array $item) use ($block) {
              return array_merge(['file' => $block['file'], 'line' => $block['line']], $item);
          }, $parsed);

      } catch (\Throwable $e) {
          fwrite(STDERR, "[review] WARNING: LLM call failed for {$block['file']}:{$block['line']}: {$e->getMessage()}\n");
          return null;
      }
  }

  // ── Main ───────────────────────────────────────────────────────────────────

  $opts = getopt('', ['diff:', 'output:']);
  $diffFile   = $opts['diff']   ?? null;
  $outputFile = $opts['output'] ?? '/tmp/orm-results.json';

  if ($diffFile === null || !file_exists($diffFile)) {
      fwrite(STDERR, "Usage: php review.php --diff=<path> --output=<path>\n");
      exit(1);
  }

  $diff   = file_get_contents($diffFile);
  $blocks = extractPhpBlocks($diff);

  if (empty($blocks)) {
      file_put_contents($outputFile, '[]');
      echo "[review] No PHP blocks found in diff. Writing empty results.\n";
      exit(0);
  }

  echo "[review] Found " . count($blocks) . " PHP block(s). Starting analysis...\n";

  $boostMcp = startBoostMcp();
  $allResults = [];

  foreach ($blocks as $block) {
      echo "[review] Reviewing {$block['file']}:{$block['line']}...\n";
      $blockResults = reviewBlock($block, $boostMcp);
      if ($blockResults !== null) {
          $allResults = array_merge($allResults, $blockResults);
      }
  }

  // Convert to (query, result) artifact format matching Python ReviewResult schema
  $artifact = array_map(function (array $item) {
      return [
          'query' => [
              'raw'        => '',          // ORM code not stored separately — file/line is the reference
              'file'       => $item['file'],
              'line'       => $item['line'],
              'suppressed' => false,
          ],
          'result' => [
              'issues'            => $item['issues']            ?? [],
              'optimized_query'   => $item['optimized_query']   ?? '',
              'index_suggestions' => $item['index_suggestions'] ?? [],
              'migration_warnings'=> $item['migration_warnings']?? [],
              'cost_analysis'     => $item['cost_analysis']     ?? [
                  'level' => 'low', 'basis' => 'static', 'reason' => '', 'estimated_improvement' => ''
              ],
              'explanation'       => $item['explanation']       ?? '',
              'suppressed'        => [],
          ],
      ];
  }, $allResults);

  file_put_contents($outputFile, json_encode($artifact, JSON_PRETTY_PRINT));
  echo "[review] Wrote " . count($artifact) . " results to {$outputFile}\n";
  ```

- [ ] **Step 2: Install PHP deps and smoke test**

  ```bash
  cd action && composer install && cd ..
  echo '+$users = User::all();' > /tmp/test-php.diff
  # Prepend the diff header so extractPhpBlocks picks it up
  cat > /tmp/test-php.diff <<'EOF'
  +++ b/app/Http/Controllers/UserController.php
  @@ -1,3 +1,5 @@
  +    public function index() {
  +        $users = User::all();
  +        return response()->json($users);
  +    }
  EOF
  ANTHROPIC_API_KEY=your_key php action/review.php --diff=/tmp/test-php.diff --output=/tmp/orm-test.json
  cat /tmp/orm-test.json
  ```
  Expected: JSON array with at least one result containing an issue.

- [ ] **Step 3: After Task 0 — wire in Boost MCP**

  In `action/review.php`, find the `startBoostMcp()` function and replace the `TODO` placeholder with the verified Relay API. Then uncomment the `withMcpServer($boostMcp)` line in `reviewBlock()`.

  Verify the change:
  ```bash
  LARAVEL_APP_PATH=/path/to/laravel-app \
  ANTHROPIC_API_KEY=your_key \
  php action/review.php --diff=/tmp/test-php.diff --output=/tmp/orm-boost-test.json
  ```
  Expected: Results with `cost_analysis.basis: "explain"` for issues where schema was checked.

- [ ] **Step 4: Commit**

  ```bash
  git add action/review.php
  git commit -m "feat: add PHP ORM reviewer script with Prism + Boost MCP integration"
  ```

---

## Task 5: Reusable GitHub Actions Workflow

**Files:**
- Create: `.github/workflows/review.yml`
- Create: `action/caller-template.yml`

- [ ] **Step 1: Create `.github/workflows/review.yml`**

  ```yaml
  # Reusable Prism Code Review Workflow
  # Called by target repos via: uses: ssaini24/prism/.github/workflows/review.yml@main
  name: Prism Code Review

  on:
    workflow_call:
      secrets:
        ANTHROPIC_API_KEY:
          required: true

  jobs:
    # ── Job 1: ORM analysis with live DB schema ──────────────────────────────
    orm-analysis:
      name: ORM Analysis (PHP + Boost MCP)
      runs-on: ubuntu-latest
      services:
        mysql:
          image: mysql:8.0
          env:
            MYSQL_ROOT_PASSWORD: secret
            MYSQL_DATABASE: testing
          ports:
            - 3306:3306
          options: >-
            --health-cmd="mysqladmin ping --silent"
            --health-interval=10s
            --health-timeout=5s
            --health-retries=5

      steps:
        - name: Checkout target repo
          uses: actions/checkout@v4
          with:
            path: target-repo

        - name: Checkout Prism repo
          uses: actions/checkout@v4
          with:
            repository: ssaini24/prism
            path: prism-action

        - name: Setup PHP 8.2
          uses: shivammathur/setup-php@v2
          with:
            php-version: "8.2"
            extensions: pdo, pdo_mysql, mbstring
            tools: composer:v2

        - name: Install target repo dependencies
          run: composer install --no-interaction --prefer-dist --no-progress
          working-directory: target-repo

        - name: Install Laravel Boost in target repo
          run: composer require laravel/boost --dev --no-interaction
          working-directory: target-repo

        - name: Run database migrations
          run: php artisan migrate --force
          working-directory: target-repo
          env:
            APP_KEY: base64:dummykey000000000000000000000000000=
            DB_CONNECTION: mysql
            DB_HOST: 127.0.0.1
            DB_PORT: 3306
            DB_DATABASE: testing
            DB_USERNAME: root
            DB_PASSWORD: secret

        - name: Install Prism PHP action dependencies
          run: composer install --no-interaction --prefer-dist --no-progress
          working-directory: prism-action/action

        - name: Fetch PR diff
          run: |
            gh api \
              repos/${{ github.repository }}/pulls/${{ github.event.pull_request.number }}/files \
              --jq '[.[].patch // ""] | join("\n")' > /tmp/pr.diff
          env:
            GH_TOKEN: ${{ github.token }}

        - name: Run ORM reviewer
          run: |
            php prism-action/action/review.php \
              --diff=/tmp/pr.diff \
              --output=/tmp/orm-results.json
          env:
            ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
            LARAVEL_APP_PATH: ${{ github.workspace }}/target-repo
            DB_CONNECTION: mysql
            DB_HOST: 127.0.0.1
            DB_PORT: 3306
            DB_DATABASE: testing
            DB_USERNAME: root
            DB_PASSWORD: secret

        - name: Upload ORM results artifact
          if: always()
          uses: actions/upload-artifact@v4
          with:
            name: orm-results
            path: /tmp/orm-results.json
            if-no-files-found: warn

    # ── Job 2: Static analysis (SQL + code review) ───────────────────────────
    static-analysis:
      name: Static Analysis (Python)
      runs-on: ubuntu-latest

      steps:
        - name: Checkout Prism repo
          uses: actions/checkout@v4
          with:
            repository: ssaini24/prism
            path: prism-action

        - name: Setup Python 3.11
          uses: actions/setup-python@v5
          with:
            python-version: "3.11"
            cache: pip
            cache-dependency-path: prism-action/requirements.txt

        - name: Install Python dependencies
          run: pip install -r requirements.txt
          working-directory: prism-action

        - name: Fetch PR diff
          run: |
            gh api \
              repos/${{ github.repository }}/pulls/${{ github.event.pull_request.number }}/files \
              --jq '[.[].patch // ""] | join("\n")' > /tmp/pr.diff
          env:
            GH_TOKEN: ${{ github.token }}

        - name: Run static analysis
          run: |
            python action/analyze.py \
              --diff=/tmp/pr.diff \
              --output=/tmp/static-results.json \
              --repo=${{ github.repository }}
          working-directory: prism-action
          env:
            LLM_PROVIDER: anthropic
            LLM_MODEL: claude-haiku-4-5-20251001
            ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
            ENABLE_ORM_REVIEW: "false"
            ENABLE_CODE_REVIEW: "false"
            ENABLE_DB_ANALYSIS_VIA_MCP: "false"

        - name: Upload static results artifact
          if: always()
          uses: actions/upload-artifact@v4
          with:
            name: static-results
            path: /tmp/static-results.json
            if-no-files-found: warn

    # ── Job 3: Post comments ─────────────────────────────────────────────────
    post-comments:
      name: Post PR Comments
      runs-on: ubuntu-latest
      needs: [orm-analysis, static-analysis]
      if: always()

      steps:
        - name: Checkout Prism repo
          uses: actions/checkout@v4
          with:
            repository: ssaini24/prism
            path: prism-action

        - name: Setup Python 3.11
          uses: actions/setup-python@v5
          with:
            python-version: "3.11"
            cache: pip
            cache-dependency-path: prism-action/requirements.txt

        - name: Install Python dependencies
          run: pip install -r requirements.txt
          working-directory: prism-action

        - name: Download ORM results
          uses: actions/download-artifact@v4
          with:
            name: orm-results
            path: /tmp
          continue-on-error: true

        - name: Download static results
          uses: actions/download-artifact@v4
          with:
            name: static-results
            path: /tmp
          continue-on-error: true

        - name: Post review comments
          run: |
            python action/post_comments.py \
              --orm-results=/tmp/orm-results.json \
              --static-results=/tmp/static-results.json \
              --owner=${{ github.repository_owner }} \
              --repo=${{ github.event.repository.name }} \
              --pr=${{ github.event.pull_request.number }} \
              --sha=${{ github.event.pull_request.head.sha }}
          working-directory: prism-action
          env:
            GITHUB_TOKEN: ${{ github.token }}
  ```

- [ ] **Step 2: Create `action/caller-template.yml`**

  This is the file target repos copy into `.github/workflows/prism-review.yml`:

  ```yaml
  # Copy this file to .github/workflows/prism-review.yml in your repo.
  #
  # Prerequisites:
  #   1. Add ANTHROPIC_API_KEY to your repo's Settings → Secrets and variables → Actions
  #   2. Ensure `php artisan migrate` works in CI (set DB env vars below if needed)
  #
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

- [ ] **Step 3: Validate workflow YAML syntax**

  ```bash
  # Install actionlint if not present
  brew install actionlint   # macOS
  actionlint .github/workflows/review.yml
  ```
  Expected: No errors.

- [ ] **Step 4: Commit**

  ```bash
  git add .github/workflows/review.yml action/caller-template.yml
  git commit -m "feat: add reusable GitHub Actions workflow for Prism code review"
  ```

---

## Task 6: End-to-End Integration Test

**Goal:** Confirm all three jobs run correctly on a real Laravel PR.

- [ ] **Step 1: Create a test Laravel repo on GitHub**

  Create a minimal public Laravel repo at e.g. `github.com/<your-username>/prism-test-laravel`.
  It only needs:
  - A basic `composer.json` with `laravel/framework`
  - A migration file in `database/migrations/`
  - A controller with at least one Eloquent query

  ```bash
  composer create-project laravel/laravel prism-test-laravel
  cd prism-test-laravel
  git init && git remote add origin git@github.com:<your-username>/prism-test-laravel.git
  ```

- [ ] **Step 2: Add caller workflow to test repo**

  ```bash
  mkdir -p .github/workflows
  cp /path/to/prism/action/caller-template.yml .github/workflows/prism-review.yml
  git add .github/workflows/prism-review.yml
  git commit -m "chore: add Prism review workflow"
  git push -u origin main
  ```

- [ ] **Step 3: Add ANTHROPIC_API_KEY secret to test repo**

  In GitHub: Test repo → Settings → Secrets → Actions → New → `ANTHROPIC_API_KEY`

- [ ] **Step 4: Open a test PR with an Eloquent N+1 query**

  ```bash
  git checkout -b test/n-plus-one
  # Add a controller with a deliberate N+1 issue:
  cat > app/Http/Controllers/UserController.php <<'PHP'
  <?php
  namespace App\Http\Controllers;
  use App\Models\User;
  class UserController extends Controller {
      public function index() {
          $users = User::all();
          foreach ($users as $user) {
              echo $user->posts->count(); // N+1: posts not eager loaded
          }
      }
  }
  PHP
  git add app/Http/Controllers/UserController.php
  git commit -m "test: add controller with N+1 issue"
  git push origin test/n-plus-one
  gh pr create --title "Test N+1 detection" --body "Testing Prism ORM review"
  ```

- [ ] **Step 5: Verify all three jobs complete**

  In GitHub Actions for the test repo:
  - `ORM Analysis (PHP + Boost MCP)` — should complete, upload `orm-results` artifact
  - `Static Analysis (Python)` — should complete, upload `static-results` artifact
  - `Post PR Comments` — should post inline comment on `UserController.php` flagging `n_plus_one_pattern`

- [ ] **Step 6: Verify PR comment content**

  The PR should show an inline comment on the `UserController.php` line with:
  - `🔴 **[n_plus_one_pattern]**` (or medium severity)
  - A suggestion to add `->with('posts')`
  - If Boost is wired up: `> 📊 _Analysis backed by live EXPLAIN data via MySQL MCP._`

---

## Open Items (from spec)

These must be resolved during Task 0 before PHP code is finalized:

1. **Boost tool names** — verify `list-tables`, `describe-table`, `table-indexes` against `laravel/boost` docs
2. **Prism Relay transport** — verify stdio MCP is supported by `echolabs/prism-relay`; update `startBoostMcp()` in `review.php`
3. **PostgreSQL support** — out of scope for v1; only MySQL 8.0 service containers are supported
