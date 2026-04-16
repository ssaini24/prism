"""CLI script for running Prism static analysis on a PR diff."""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the project root importable when this script is invoked from the action/ subdirectory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.analyser import Analyser  # noqa: E402


def run(diff_path: str, output_path: str, repo: str = "") -> None:
    """Read a diff file, run analysis, and write JSON results to output_path."""
    with open(diff_path, "r", encoding="utf-8") as f:
        diff_text = f.read()

    analyser = Analyser()
    pairs = analyser.analyse_pr(diff_text, repo=repo)

    data = [
        {"query": query.model_dump(), "result": result.model_dump()}
        for query, result in pairs
    ]

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze a PR diff for SQL/ORM issues and write results as JSON."
    )
    parser.add_argument("--diff", required=True, help="Path to the unified diff file")
    parser.add_argument("--output", required=True, help="Path to write the JSON results")
    parser.add_argument("--repo", default="", help="Repository in owner/repo format")
    args = parser.parse_args()

    run(args.diff, args.output, repo=args.repo)


if __name__ == "__main__":
    main()
