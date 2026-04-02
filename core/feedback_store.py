"""
Feedback loop store — persists engineer signals on Prism review comments.

Engineers reply to Prism inline comments with natural language:
  "false positive", "intentional", "by design"  →  negative signal (-1)
  "good catch", "fixed", "confirmed", "valid"    →  positive signal (+1)

Signals are stored per (rule, repo, path_context) where path_context is the
leading directory of the file (e.g. "database/migrations", "app/Http/Controllers").
This means the same rule can be marked as intentional in one context and real in another.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "feedback.db"

# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

_NEGATIVE = re.compile(
    r"\b(false.?positive|fp|intentional|by.?design|won.?t.?fix|wontfix|"
    r"not.?applicable|n/?a|ignore|expected.?behavior|this.?is.?fine|ok.?here)\b",
    re.IGNORECASE,
)
_POSITIVE = re.compile(
    r"\b(good.?catch|fixed|confirmed|valid|you.?re.?right|correct|agreed|"
    r"thanks|thank.?you|will.?fix|addressing.?this)\b",
    re.IGNORECASE,
)

_SKIP_THRESHOLD = -1  # one false positive = skip in this context going forward


# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                rule         TEXT    NOT NULL,
                repo         TEXT    NOT NULL DEFAULT '',
                path_context TEXT    NOT NULL DEFAULT '',
                signal       INTEGER NOT NULL,
                comment      TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrate old schema: file_ext → path_context
        cols = {row[1] for row in conn.execute("PRAGMA table_info(feedback)")}
        if "file_ext" in cols and "path_context" not in cols:
            conn.execute("ALTER TABLE feedback RENAME COLUMN file_ext TO path_context")
            logger.info("Migrated feedback table: file_ext → path_context")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rule_repo_ctx "
            "ON feedback(rule, repo, path_context)"
        )
    logger.info("Feedback DB initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_signal(text: str) -> int | None:
    if _NEGATIVE.search(text):
        return -1
    if _POSITIVE.search(text):
        return +1
    return None


def record_feedback(
    rule: str,
    repo: str,
    file_path: str,
    signal: int,
    comment: str = "",
) -> None:
    ctx = _path_context(file_path)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO feedback (rule, repo, path_context, signal, comment) "
            "VALUES (?, ?, ?, ?, ?)",
            (rule, repo, ctx, signal, comment[:500]),
        )
    direction = "positive ✓" if signal > 0 else "negative ✗"
    logger.info(
        "Feedback recorded: rule=%s repo=%s context=%s signal=%s",
        rule, repo, ctx or "(root)", direction,
    )


def get_adjustment(rule: str, repo: str, file_path: str) -> dict:
    """
    Return adjustment metadata for a rule given the file it fired in.

    Checks learned feedback from the team (feedback.db only — no hardcoded rules).

    Returns:
        {
          "net_signal":     int,
          "severity_delta": 0,
          "skip":           bool,
          "label":          str | None,
          "source":         "learned" | "none",
        }
    """
    ctx = _path_context(file_path)

    # Exact repo+context match, then repo-wide fallback
    net = _query_net_signal(rule, repo, ctx)
    if net == 0 and ctx:
        net = _query_net_signal(rule, repo, "")

    if net <= _SKIP_THRESHOLD:
        return {
            "net_signal": net,
            "severity_delta": 0,
            "skip": True,
            "label": None,
            "source": "learned",
        }

    if net >= 3:
        return {
            "net_signal": net,
            "severity_delta": 0,
            "skip": False,
            "label": f"_Confirmed issue pattern for your team in `{ctx or 'this repo'}` ({net}x)._",
            "source": "learned",
        }

    return {"net_signal": net, "severity_delta": 0, "skip": False, "label": None, "source": "none"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _path_context(file_path: str) -> str:
    """
    Extract the leading directory from a file path as context.

    "database/migrations/2024_01_create_users.php" → "database/migrations"
    "app/Http/Controllers/UserController.php"      → "app/Http/Controllers"
    "tests/Feature/UserTest.php"                   → "tests"
    "main.go"                                       → ""
    """
    parts = Path(file_path).parts
    if len(parts) <= 1:
        return ""
    return str(Path(*parts[:-1]))


def _query_net_signal(rule: str, repo: str, path_context: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(signal), 0) as net FROM feedback "
            "WHERE rule = ? AND (repo = '' OR repo = ?) AND path_context = ?",
            (rule, repo, path_context),
        ).fetchone()
    return int(row["net"]) if row else 0


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
