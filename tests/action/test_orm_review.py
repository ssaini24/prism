"""Tests for action/orm_review.py"""
from __future__ import annotations
import json, os, sys
from unittest.mock import MagicMock, patch
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
