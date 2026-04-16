# ORM Context-Aware Analysis — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reusable GitHub Actions workflow to Prism that gives the LLM live database schema access (via Laravel Boost MCP) when reviewing Eloquent ORM code in PHP PRs.

**Architecture:** Three parallel jobs — `orm-analysis` (Python + `claude -p` + Boost MCP stdio + real MySQL) and `static-analysis` (existing Python Prism static rules) run in parallel; `post-comments` waits for both, merges results, and posts all PR comments via the existing Python commenter. Target repos add a single 5-line caller workflow. **Prism repo stays Python-only** — no PHP code in Prism. Boost runs in the target repo's CI environment; Prism calls it via `claude -p --mcp-config`.

**Tech Stack:** Python 3.11, `laravel/boost` (MCP schema server, installed in target repo), `claude` CLI (`claude -p --mcp-config`), GitHub Actions reusable workflows, pytest, MySQL 8.0 service containers.

**Verified facts (from web research):**
- `laravel/boost` requires Laravel 10+ — gracefully skipped for Laravel 8 repos
- Boost MCP server started with: `php artisan boost:mcp` (stdio transport)
- Boost tool names: `database-schema`, `database-query`, `application-info`, `database-connections`
- `claude -p` supports `--mcp-config <json-file>` flag for stdio MCP servers
- Prism relay package exists (`prism-php/relay`) but is NOT needed — we use `claude -p` subprocess pattern instead (same as `core/db_explainer.py`)

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `action/analyze.py` | **Create** | Python CLI: parse diff → run Analyser → write results JSON |
| `action/post_comments.py` | **Create** | Load both artifacts → call PRCommenter → post PR comments |
| `action/orm_review.py` | **Create** | Python ORM reviewer: extract PHP blocks → `claude -p --mcp-config` with Boost → write JSON |
| `.github/workflows/review.yml` | **Create** | Reusable 3-job workflow definition |
| `action/caller-template.yml` | **Create** | Copy-paste template for target repos |
| `tests/__init__.py` | **Create** | Makes tests/ a package |
| `tests/action/__init__.py` | **Create** | Makes tests/action/ a package |
| `tests/action/test_analyze.py` | **Create** | Tests for action/analyze.py |
| `tests/action/test_post_comments.py` | **Create** | Tests for action/post_comments.py |
| `tests/action/test_orm_review.py` | **Create** | Tests for action/orm_review.py |
| `Dockerfile` | **No change** | Unchanged — workflow runs Python directly, no Docker needed |

---

## Task 1: Python Static Analysis CLI Script

**Files:**
- Create: `action/__init__.py`
- Create: `action/analyze.py`
- Create: `tests/__init__.py`
- Create: `tests/action/__init__.py`
- Create: `tests/action/test_analyze.py`

- [ ] **Step 1: Write failing tests**

  Create `tests/__init__.py` (empty), `tests/action/__init__.py` (empty), then `tests/action/test_analyze.py`:

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
      assert data[0]["result"]["issues"][0]["type"] == "select_star"


  def test_analyze_passes_repo_to_analyser(tmp_path):
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

  Create `action/__init__.py` (empty), then `action/analyze.py`:

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
  Expected: 3 tests pass.

- [ ] **Step 5: Smoke test with a real diff**

  ```bash
  echo '+SELECT * FROM users;' > /tmp/test.diff
  LLM_PROVIDER=claude-code python action/analyze.py \
    --diff=/tmp/test.diff --output=/tmp/test-results.json
  cat /tmp/test-results.json
  ```
  Expected: JSON array with at least one `select_star` issue.

- [ ] **Step 6: Commit**

  ```bash
  git add action/__init__.py action/analyze.py tests/__init__.py tests/action/__init__.py tests/action/test_analyze.py
  git commit -m "feat: add action/analyze.py CLI for static analysis"
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
          "query": {"raw": "SELECT * FROM users", "file": "queries.sql", "line": 1, "suppressed": False},
          "result": {
              "issues": [{"type": "select_star", "severity": "medium", "confidence": "high",
                          "line": 1, "description": "Avoid SELECT *", "suggestion": "Specify columns"}],
              "optimized_query": "SELECT id, name FROM users",
              "index_suggestions": [],
              "migration_warnings": [],
              "cost_analysis": {"level": "medium", "basis": "static", "reason": "select_star", "estimated_improvement": ""},
              "explanation": "SELECT * detected.",
              "suppressed": [],
          },
      }
  ]


  def test_load_artifact_returns_empty_for_missing_file():
      from action.post_comments import load_artifact
      assert load_artifact("/nonexistent/path.json") == []


  def test_load_artifact_deserializes_correctly(tmp_path):
      from action.post_comments import load_artifact
      from models.review import ExtractedQuery, ReviewResult

      f = tmp_path / "results.json"
      f.write_text(json.dumps(SAMPLE_ARTIFACT))

      pairs = load_artifact(str(f))
      assert len(pairs) == 1
      query, result = pairs[0]
      assert isinstance(query, ExtractedQuery)
      assert isinstance(result, ReviewResult)
      assert query.raw == "SELECT * FROM users"
      assert result.issues[0].type == "select_star"


  def test_post_merges_both_artifacts(tmp_path):
      from action.post_comments import post

      orm = tmp_path / "orm.json"
      static = tmp_path / "static.json"
      orm.write_text(json.dumps(SAMPLE_ARTIFACT))
      static.write_text(json.dumps(SAMPLE_ARTIFACT))

      with patch("action.post_comments.PRCommenter") as MockCommenter:
          mock_instance = MagicMock()
          MockCommenter.return_value = mock_instance

          post(orm_results=str(orm), static_results=str(static),
               owner="owner", repo="repo", pr_number=1, sha="abc")

          call_kwargs = mock_instance.post_review.call_args.kwargs
          assert len(call_kwargs["results"]) == 2


  def test_post_works_with_one_missing_artifact(tmp_path):
      from action.post_comments import post

      static = tmp_path / "static.json"
      static.write_text(json.dumps(SAMPLE_ARTIFACT))

      with patch("action.post_comments.PRCommenter") as MockCommenter:
          mock_instance = MagicMock()
          MockCommenter.return_value = mock_instance

          post(orm_results="/nonexistent.json", static_results=str(static),
               owner="owner", repo="repo", pr_number=1, sha="abc")

          call_kwargs = mock_instance.post_review.call_args.kwargs
          assert len(call_kwargs["results"]) == 1
  ```

- [ ] **Step 2: Run tests to confirm they fail**

  ```bash
  pytest tests/action/test_post_comments.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'action.post_comments'`

- [ ] **Step 3: Implement `action/post_comments.py`**

  ```python
  """Merges ORM and static analysis artifacts and posts PR review comments.

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
              print(f"[post_comments] Skipping malformed entry: {exc}", file=sys.stderr)
      return pairs


  def post(orm_results: str, static_results: str, owner: str,
           repo: str, pr_number: int, sha: str) -> None:
      results = load_artifact(orm_results) + load_artifact(static_results)
      print(f"[post_comments] Posting review for {len(results)} blocks.")
      PRCommenter().post_review(
          owner=owner, repo_name=repo, pr_number=pr_number,
          results=results, commit_sha=sha,
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
      post(orm_results=args.orm_results, static_results=args.static_results,
           owner=args.owner, repo=args.repo, pr_number=args.pr, sha=args.sha)


  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 4: Run all tests**

  ```bash
  pytest tests/ -v
  ```
  Expected: All 7 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add action/post_comments.py tests/action/test_post_comments.py
  git commit -m "feat: add action/post_comments.py to merge artifacts and post PR review"
  ```

---

## Task 3: Python ORM Reviewer (claude -p + Boost MCP)

**Files:**
- Create: `action/orm_review.py`
- Create: `tests/action/test_orm_review.py`

**Context:** This replaces the original PHP reviewer design. Instead of running a PHP script with prism-php/relay, we use the same `claude -p` subprocess pattern already used in `core/db_explainer.py`. The workflow installs `laravel/boost --dev` in the target repo's CI, runs `php artisan migrate`, then `orm_review.py` writes a Boost MCP config JSON and calls `claude -p --mcp-config` with the PHP diff. Claude handles the Boost tool-calling loop internally. For repos that can't install Boost (e.g. Laravel 8), the script falls back to LLM-only analysis with all issues marked `confidence: low`.

**Boost MCP config shape (written at runtime):**
```json
{
  "mcpServers": {
    "laravel-boost": {
      "command": "php",
      "args": ["/path/to/artisan", "boost:mcp"]
    }
  }
}
```

**claude -p invocation:**
```bash
claude -p "<prompt>" \
  --mcp-config /tmp/boost-mcp.json \
  --allowedTools "mcp__laravel-boost__database-schema,mcp__laravel-boost__database-query,mcp__laravel-boost__application-info"
```

- [ ] **Step 1: Write failing tests**

  Create `tests/action/test_orm_review.py`:

  ```python
  """Tests for action/orm_review.py"""
  from __future__ import annotations

  import json
  import os
  import sys
  from unittest.mock import MagicMock, patch, call
  import subprocess

  import pytest

  sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

  SAMPLE_PHP_DIFF = """\
  +++ b/app/Http/Controllers/UserController.php
  @@ -1,5 +1,8 @@
  +    public function index() {
  +        $users = User::all();
  +        foreach ($users as $user) {
  +            echo $user->posts->count();
  +        }
  +    }
  """

  LLM_RESPONSE = json.dumps([{
      "file": "app/Http/Controllers/UserController.php",
      "line": 1,
      "issues": [{"type": "n_plus_one_pattern", "severity": "high", "confidence": "high",
                  "line": 3, "description": "posts loaded in loop", "suggestion": "Use with('posts')"}],
      "optimized_query": "User::with('posts')->get()",
      "index_suggestions": [],
      "migration_warnings": [],
      "cost_analysis": {"level": "high", "basis": "explain", "reason": "N+1", "estimated_improvement": ""},
      "explanation": "N+1 detected.",
  }])


  def test_extract_php_blocks_returns_blocks_for_php_files():
      from action.orm_review import extract_php_blocks
      blocks = extract_php_blocks(SAMPLE_PHP_DIFF)
      assert len(blocks) == 1
      assert blocks[0]["file"] == "app/Http/Controllers/UserController.php"
      assert "User::all()" in blocks[0]["raw"]


  def test_extract_php_blocks_ignores_non_php_files():
      from action.orm_review import extract_php_blocks
      diff = """\
  +++ b/queries.sql
  @@ -1,3 +1,4 @@
  +SELECT * FROM users;
  """
      blocks = extract_php_blocks(diff)
      assert blocks == []


  def test_write_boost_config(tmp_path):
      from action.orm_review import write_boost_config
      artisan = "/path/to/artisan"
      config_path = write_boost_config(str(tmp_path), artisan)
      config = json.loads(open(config_path).read())
      assert config["mcpServers"]["laravel-boost"]["command"] == "php"
      assert artisan in config["mcpServers"]["laravel-boost"]["args"]


  def test_review_blocks_writes_results(tmp_path):
      from action.orm_review import review_blocks

      blocks = [{"file": "app/Http/Controllers/UserController.php", "line": 1,
                 "raw": "$users = User::all();"}]
      output_file = str(tmp_path / "orm-results.json")
      config_path = str(tmp_path / "boost.json")

      with patch("action.orm_review.call_llm_with_boost") as mock_call:
          mock_call.return_value = json.loads(LLM_RESPONSE)
          review_blocks(blocks, config_path, output_file)

      data = json.loads(open(output_file).read())
      assert len(data) == 1
      assert data[0]["result"]["issues"][0]["type"] == "n_plus_one_pattern"


  def test_review_blocks_writes_empty_for_no_blocks(tmp_path):
      from action.orm_review import review_blocks
      output_file = str(tmp_path / "orm-results.json")
      review_blocks([], "/tmp/boost.json", output_file)
      data = json.loads(open(output_file).read())
      assert data == []
  ```

- [ ] **Step 2: Run tests to confirm they fail**

  ```bash
  pytest tests/action/test_orm_review.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'action.orm_review'`

- [ ] **Step 3: Implement `action/orm_review.py`**

  ```python
  """ORM reviewer using claude -p + Laravel Boost MCP.

  Usage:
      python action/orm_review.py \
          --diff /tmp/pr.diff \
          --output /tmp/orm-results.json \
          --laravel-path /path/to/laravel-app

  Environment variables:
      ANTHROPIC_API_KEY — passed through to claude subprocess

  If laravel/boost is not installed in the target app, the script falls back
  to LLM-only analysis (no schema tools) and marks all issues confidence: low.
  """
  from __future__ import annotations

  import argparse
  import json
  import os
  import re
  import subprocess
  import sys
  import time

  sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

  SYSTEM_PROMPT = """\
  You are a Senior Laravel ORM Specialist reviewing PHP code from a pull request diff.

  You have access to live database schema through Laravel Boost MCP tools.
  Before flagging any issue involving a table name, column, or index — call the
  relevant tool to verify against the actual migrated schema.

  Available tools: database-schema, database-query, application-info.

  Objectives:
  1. Schema Validation: call database-schema to verify tables and columns exist.
     Flag missing tables/columns as severity: high.
  2. N+1 Detection: identify loops accessing relationships without eager loading.
     Suggest the correct ->with() call.
  3. Column Selection: flag select() or pluck() opportunities for large datasets.
  4. Performance: suggest chunk(), lazy(), or cursor() over get() where appropriate.
  5. Modern Standards: flag non-idiomatic Laravel patterns.

  Rules:
  - Never assume a table/column exists — call a tool to verify.
  - Set confidence: low if schema could not be verified (tool unavailable).
  - Set cost_analysis.basis to "explain" when backed by schema/index data from
    tools; "static" otherwise.

  Output ONLY a valid JSON array — no markdown, no prose:
  [
    {
      "file": "<filename>",
      "line": <int>,
      "issues": [
        {
          "type": "<issue_type>",
          "severity": "low|medium|high",
          "confidence": "low|medium|high",
          "line": <int>,
          "description": "<string>",
          "suggestion": "<string>"
        }
      ],
      "optimized_query": "<string or empty>",
      "index_suggestions": ["<CREATE INDEX ...>"],
      "migration_warnings": [],
      "cost_analysis": {
        "level": "low|medium|high",
        "basis": "static|explain",
        "reason": "<string>",
        "estimated_improvement": "<string>"
      },
      "explanation": "<2-3 sentences>"
    }
  ]

  Valid issue types: select_star, missing_where_clause, function_on_indexed_column,
  join_without_condition, n_plus_one_pattern, destructive_ddl, unsafe_alter_table,
  full_table_scan, missing_index, inefficient_subquery, implicit_type_conversion,
  unbounded_result_set.

  Return [] if no issues are found.
  """


  def extract_php_blocks(diff: str) -> list[dict]:
      """Extract added PHP code blocks from a unified diff."""
      blocks: list[dict] = []
      current_file: str | None = None
      current_line = 0
      raw_lines: list[str] = []
      block_start = 0

      for line in diff.splitlines():
          if line.startswith("+++ b/"):
              if current_file and raw_lines and current_file.endswith(".php"):
                  blocks.append({"file": current_file, "line": block_start,
                                  "raw": "\n".join(raw_lines)})
              current_file = line[6:].strip()
              current_line = 0
              raw_lines = []
              block_start = 0
          elif line.startswith("@@"):
              m = re.search(r"\+(\d+)", line)
              if m:
                  current_line = int(m.group(1)) - 1
          elif line.startswith("+") and not line.startswith("+++"):
              current_line += 1
              if block_start == 0:
                  block_start = current_line
              raw_lines.append(line[1:])
          elif not line.startswith("-"):
              current_line += 1

      if current_file and raw_lines and current_file.endswith(".php"):
          blocks.append({"file": current_file, "line": block_start,
                          "raw": "\n".join(raw_lines)})
      return blocks


  def write_boost_config(work_dir: str, artisan_path: str) -> str:
      """Write a claude MCP config JSON pointing to the Boost stdio server."""
      config = {
          "mcpServers": {
              "laravel-boost": {
                  "command": "php",
                  "args": [artisan_path, "boost:mcp"],
              }
          }
      }
      path = os.path.join(work_dir, "boost-mcp-config.json")
      with open(path, "w") as f:
          json.dump(config, f)
      return path


  def boost_available(artisan_path: str) -> bool:
      """Return True if laravel/boost is installed in the target app."""
      try:
          result = subprocess.run(
              ["php", artisan_path, "list", "--format=json"],
              capture_output=True, text=True, timeout=15,
          )
          return "boost:mcp" in result.stdout
      except Exception:
          return False


  def call_llm_with_boost(block: dict, config_path: str | None) -> list[dict]:
      """Call claude -p with Boost MCP tools for one PHP block."""
      user_prompt = (
          f"Review this PHP/Eloquent code from `{block['file']}` (line {block['line']}):\n\n"
          f"```php\n{block['raw']}\n```"
      )
      full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"

      cmd = ["claude", "-p", full_prompt]
      if config_path:
          cmd += [
              "--mcp-config", config_path,
              "--allowedTools",
              "mcp__laravel-boost__database-schema,"
              "mcp__laravel-boost__database-query,"
              "mcp__laravel-boost__application-info",
          ]

      t0 = time.monotonic()
      try:
          proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
          elapsed = time.monotonic() - t0

          if proc.returncode != 0:
              print(f"[orm_review] claude failed ({elapsed:.1f}s): {proc.stderr[:200]}",
                    file=sys.stderr)
              return []

          output = proc.stdout.strip()
          # Strip markdown fences if present
          cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", output).strip()
          match = re.search(r"\[[\s\S]*\]", cleaned)
          if not match:
              print(f"[orm_review] No JSON array in response for {block['file']}",
                    file=sys.stderr)
              return []

          parsed = json.loads(match.group(0))
          print(f"[orm_review] {block['file']}:{block['line']} → "
                f"{len(parsed)} result(s) in {elapsed:.1f}s")
          return parsed

      except subprocess.TimeoutExpired:
          print(f"[orm_review] Timeout for {block['file']}:{block['line']}", file=sys.stderr)
          return []
      except Exception as exc:
          print(f"[orm_review] Error for {block['file']}:{block['line']}: {exc}",
                file=sys.stderr)
          return []


  def review_blocks(blocks: list[dict], config_path: str | None, output_path: str) -> None:
      """Review all PHP blocks and write results artifact."""
      if not blocks:
          with open(output_path, "w") as f:
              json.dump([], f)
          print("[orm_review] No PHP blocks. Writing empty results.")
          return

      artifact = []
      for block in blocks:
          llm_results = call_llm_with_boost(block, config_path)
          for item in llm_results:
              artifact.append({
                  "query": {
                      "raw": block["raw"],
                      "file": item.get("file", block["file"]),
                      "line": item.get("line", block["line"]),
                      "suppressed": False,
                  },
                  "result": {
                      "issues":             item.get("issues", []),
                      "optimized_query":    item.get("optimized_query", ""),
                      "index_suggestions":  item.get("index_suggestions", []),
                      "migration_warnings": item.get("migration_warnings", []),
                      "cost_analysis":      item.get("cost_analysis", {
                          "level": "low", "basis": "static",
                          "reason": "", "estimated_improvement": "",
                      }),
                      "explanation":        item.get("explanation", ""),
                      "suppressed":         [],
                  },
              })

      with open(output_path, "w") as f:
          json.dump(artifact, f, indent=2)
      print(f"[orm_review] Wrote {len(artifact)} results to {output_path}")


  def main() -> None:
      parser = argparse.ArgumentParser()
      parser.add_argument("--diff", required=True)
      parser.add_argument("--output", required=True)
      parser.add_argument("--laravel-path", default="")
      args = parser.parse_args()

      with open(args.diff) as f:
          diff_text = f.read()

      blocks = extract_php_blocks(diff_text)
      print(f"[orm_review] Found {len(blocks)} PHP block(s).")

      config_path = None
      if args.laravel_path:
          artisan = os.path.join(args.laravel_path, "artisan")
          if boost_available(artisan):
              config_path = write_boost_config(os.path.dirname(args.output), artisan)
              print(f"[orm_review] Boost available — schema tools enabled.")
          else:
              print("[orm_review] Boost not available — running LLM-only (confidence: low).",
                    file=sys.stderr)

      review_blocks(blocks, config_path, args.output)


  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 4: Run tests to confirm they pass**

  ```bash
  pytest tests/action/test_orm_review.py -v
  ```
  Expected: All 5 tests pass.

- [ ] **Step 5: Run all tests**

  ```bash
  pytest tests/ -v
  ```
  Expected: All 12 tests pass.

- [ ] **Step 6: Commit**

  ```bash
  git add action/orm_review.py tests/action/test_orm_review.py
  git commit -m "feat: add action/orm_review.py — Python ORM reviewer via claude -p + Boost MCP"
  ```

---

## Task 4: Reusable GitHub Actions Workflow

**Files:**
- Create: `.github/workflows/review.yml`
- Create: `action/caller-template.yml`

**Context:** Three jobs. Job 1 (`orm-analysis`) runs `action/orm_review.py` with MySQL service + optional Boost. Job 2 (`static-analysis`) runs `action/analyze.py` for SQL/code files. Job 3 (`post-comments`) merges both artifacts and calls `action/post_comments.py`. Jobs 1 and 2 run in parallel. Job 3 has `needs: [orm-analysis, static-analysis]` and `if: always()` so it runs even if one job fails.

- [ ] **Step 1: Create `.github/workflows/review.yml`**

  ```yaml
  # Reusable Prism Code Review Workflow
  # Target repos use: uses: ssaini24/prism/.github/workflows/review.yml@main
  name: Prism Code Review

  on:
    workflow_call:
      secrets:
        ANTHROPIC_API_KEY:
          required: true

  jobs:
    # ── Job 1: ORM analysis (PHP + optional Boost MCP + real MySQL) ──────────
    orm-analysis:
      name: ORM Analysis
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

        - name: Install target repo PHP dependencies
          run: composer install --no-interaction --prefer-dist --no-progress
          working-directory: target-repo

        - name: Install Laravel Boost (skipped if incompatible)
          run: composer require laravel/boost --dev --no-interaction || echo "Boost install skipped"
          working-directory: target-repo

        - name: Run database migrations
          run: php artisan migrate --force
          working-directory: target-repo
          env:
            APP_KEY: ${{ secrets.APP_KEY || 'base64:dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleXQ=' }}
            DB_CONNECTION: mysql
            DB_HOST: 127.0.0.1
            DB_PORT: 3306
            DB_DATABASE: testing
            DB_USERNAME: root
            DB_PASSWORD: secret

        - name: Setup Python 3.11
          uses: actions/setup-python@v5
          with:
            python-version: "3.11"

        - name: Install Prism Python dependencies
          run: pip install -r requirements.txt
          working-directory: prism-action

        - name: Fetch PR diff
          run: |
            gh api repos/${{ github.repository }}/pulls/${{ github.event.pull_request.number }}/files \
              --jq '[.[].patch // ""] | join("\n")' > /tmp/pr.diff
          env:
            GH_TOKEN: ${{ github.token }}

        - name: Run ORM reviewer
          run: |
            python prism-action/action/orm_review.py \
              --diff=/tmp/pr.diff \
              --output=/tmp/orm-results.json \
              --laravel-path=${{ github.workspace }}/target-repo
          env:
            ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
            DB_CONNECTION: mysql
            DB_HOST: 127.0.0.1
            DB_PORT: 3306
            DB_DATABASE: testing
            DB_USERNAME: root
            DB_PASSWORD: secret

        - name: Upload ORM results
          if: always()
          uses: actions/upload-artifact@v4
          with:
            name: orm-results
            path: /tmp/orm-results.json
            if-no-files-found: warn

    # ── Job 2: Static analysis (SQL + other code files) ──────────────────────
    static-analysis:
      name: Static Analysis
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

        - name: Install Prism Python dependencies
          run: pip install -r requirements.txt
          working-directory: prism-action

        - name: Fetch PR diff
          run: |
            gh api repos/${{ github.repository }}/pulls/${{ github.event.pull_request.number }}/files \
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

        - name: Upload static results
          if: always()
          uses: actions/upload-artifact@v4
          with:
            name: static-results
            path: /tmp/static-results.json
            if-no-files-found: warn

    # ── Job 3: Post PR comments ───────────────────────────────────────────────
    post-comments:
      name: Post Review Comments
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

        - name: Install Prism Python dependencies
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

  ```yaml
  # Copy to .github/workflows/prism-review.yml in your repo.
  #
  # Required secrets in your repo (Settings → Secrets → Actions):
  #   ANTHROPIC_API_KEY — your Anthropic API key
  #   APP_KEY           — your Laravel APP_KEY (for running migrations in CI)
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
  brew install actionlint
  actionlint .github/workflows/review.yml
  ```
  Expected: No errors output.

- [ ] **Step 4: Commit**

  ```bash
  git add .github/workflows/review.yml action/caller-template.yml
  git commit -m "feat: add reusable GitHub Actions workflow for Prism code review"
  ```

---

## Task 5: Add Caller Workflow to Laravel-API Test Repo

**Target repo:** `/Users/sahil/ai-projects/Laravel-API` (GitHub: `ssaini24/Laravel-API`)
**Note:** Laravel-API uses Laravel 8 — Boost will not install (requires Laravel 10+). The ORM reviewer will run LLM-only with `confidence: low`. Static analysis (SQL rules) will run normally.

- [ ] **Step 1: Create caller workflow in Laravel-API**

  ```bash
  mkdir -p /Users/sahil/ai-projects/Laravel-API/.github/workflows
  ```

  Create `/Users/sahil/ai-projects/Laravel-API/.github/workflows/prism-review.yml`:

  ```yaml
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

- [ ] **Step 2: Add ANTHROPIC_API_KEY secret to Laravel-API repo**

  In GitHub: `ssaini24/Laravel-API` → Settings → Secrets and variables → Actions → New repository secret:
  - Name: `ANTHROPIC_API_KEY`
  - Value: your Anthropic API key

- [ ] **Step 3: Commit and push caller workflow**

  ```bash
  cd /Users/sahil/ai-projects/Laravel-API
  git add .github/workflows/prism-review.yml
  git commit -m "chore: add Prism code review workflow"
  git push origin main
  ```

- [ ] **Step 4: Open a test PR with an ORM issue**

  ```bash
  cd /Users/sahil/ai-projects/Laravel-API
  git checkout -b test/prism-orm-review
  # The existing UserController.php already has N+1, select_star, destructive_ddl issues
  # Just make a small edit to trigger the workflow
  echo "// test prism review" >> app/Http/Controllers/UserController.php
  git add app/Http/Controllers/UserController.php
  git commit -m "test: trigger Prism review on UserController"
  git push origin test/prism-orm-review
  gh pr create --title "Test: Prism ORM review" --body "Testing Prism workflow integration"
  ```

- [ ] **Step 5: Verify all three jobs run in GitHub Actions**

  Open the PR on GitHub. Under the Checks tab, verify:
  - `ORM Analysis` — completes, uploads `orm-results` artifact
  - `Static Analysis` — completes, uploads `static-results` artifact
  - `Post Review Comments` — posts inline comments on `UserController.php`

  Expected comments: `missing_where_clause` on `deleteAll()`, `destructive_ddl` on `dropSessions()`, N+1 pattern on `withOrderCount()`.

- [ ] **Step 6: Commit Laravel-API changes (already done in Step 3)**

  No additional commit needed.
