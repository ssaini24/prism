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
    from models.review import ExtractedQuery, Issue, ReviewResult
    diff_file = tmp_path / "test.diff"
    diff_file.write_text(SAMPLE_SQL_DIFF)
    output_file = tmp_path / "results.json"
    query = ExtractedQuery(raw="SELECT * FROM users", file="queries.sql", line=1)
    result = ReviewResult(
        issues=[Issue(type="select_star", severity="medium", confidence="high",
                      line=1, description="Avoid SELECT *", suggestion="Specify columns")],
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
