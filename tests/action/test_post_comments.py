"""Tests for action/post_comments.py"""
from __future__ import annotations
import json, os, sys
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
