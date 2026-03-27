"""
Phase 2: EXPLAIN JSON output parser.

This module is a placeholder for Phase 2 functionality.
It will parse PostgreSQL/MySQL EXPLAIN (FORMAT JSON) output to extract:
- Sequential scans on large tables
- Missing index usage
- High row estimate errors
- Nested loop join costs

Not used in Phase 1 MVP.
"""
from __future__ import annotations


def parse_explain_json(explain_output: dict) -> list[dict]:
    """
    Parse EXPLAIN JSON output and return a list of findings.

    Phase 2 implementation only — raises NotImplementedError in Phase 1.
    """
    raise NotImplementedError("EXPLAIN parsing is a Phase 2 feature.")
