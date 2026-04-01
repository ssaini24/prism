"""
Feedback loop store — persists engineer signals on Prism review comments.

Engineers reply to Prism inline comments with natural language:
  "false positive", "intentional", "by design"  →  negative signal (-1)
  "good catch", "fixed", "confirmed", "valid"    →  positive signal (+1)

Signals are stored per (rule, repo, path_context) where path_context is the
leading directory of the file (e.g. "database/migrations", "app/Http/Controllers").
This means the same rule can be marked as intentional in one context and real in another.

Built-in suppressions handle universally known safe patterns so engineers
don't have to teach Prism from scratch on every repo.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "feedback.db"

# ---------------------------------------------------------------------------
# Built-in suppressions
# Hardcoded known-safe contexts — no feedback needed, Prism already knows.
# Format: {rule: [(path_prefix, note), ...]}
# ---------------------------------------------------------------------------
_BUILT_IN_SUPPRESSIONS: dict[str, list[tuple[str, str]]] = {
    "destructive_ddl": [
        (
            "database/migrations",
            "DROP/TRUNCATE inside a migration file is expected — "
            "this is likely the `down()` rollback method.",
        ),
        (
            "database/seeders",
            "Destructive DDL in a seeder is expected for test data setup.",
        ),
        (
            "tests",
            "Destructive DDL in test files is expected for fixture teardown.",
        ),
    ],
    "missing_where_clause": [
        (
            "database/seeders",
            "Unbounded UPDATE/DELETE in a seeder is expected — seeders truncate and reload test data.",
        ),
        (
            "tests",
            "Unbounded UPDATE/DELETE in test files is expected for fixture cleanup.",
        ),
    ],
    "unsafe_alter_table": [
        (
            "database/migrations",
            "ALTER TABLE in migrations is expected. "
            "Ensure ALGORITHM=INPLACE, LOCK=NONE are set for large tables.",
        ),
    ],
}

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

_DOWNGRADE_THRESHOLD = -1
_SUPPRESS_THRESHOLD  = -2
_SKIP_THRESHOLD      = -3


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

    Checks built-in suppressions first (context-aware hardcoded rules),
    then learned feedback from the team.

    Returns:
        {
          "net_signal":     int,
          "severity_delta": -1 | 0,
          "label":          str | None,
          "source":         "builtin" | "learned" | "none",
        }
    """
    ctx = _path_context(file_path)

    # 1. Check built-in suppressions
    builtin_note = _check_builtin(rule, file_path)
    if builtin_note:
        return {
            "net_signal": -1,
            "severity_delta": -1,
            "label": f"ℹ️ {builtin_note}",
            "source": "builtin",
        }

    # 2. Check learned feedback (exact repo+context, then cross-context fallback)
    net = _query_net_signal(rule, repo, ctx)
    if net == 0 and ctx:
        net = _query_net_signal(rule, repo, "")  # repo-wide fallback

    if net <= _SKIP_THRESHOLD:
        return {
            "net_signal": net,
            "severity_delta": -1,
            "skip": True,
            "label": None,
            "source": "learned",
        }
    if net <= _SUPPRESS_THRESHOLD:
        return {
            "net_signal": net,
            "severity_delta": -1,
            "skip": False,
            "label": f"⚠️ Your team has flagged this as a false positive {abs(net)} time(s) in `{ctx or 'this repo'}` — review carefully.",
            "source": "learned",
        }
    if net <= _DOWNGRADE_THRESHOLD:
        return {
            "net_signal": net,
            "severity_delta": -1,
            "label": f"_Your team has marked similar findings as false positives in `{ctx or 'this repo'}` ({abs(net)}x)._",
            "source": "learned",
        }
    if net >= 3:
        return {
            "net_signal": net,
            "severity_delta": 0,
            "label": f"_Confirmed issue pattern for your team in `{ctx or 'this repo'}` ({net}x)._",
            "source": "learned",
        }

    return {"net_signal": net, "severity_delta": 0, "label": None, "source": "none"}


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
    # Use up to the last directory component (not the filename)
    return str(Path(*parts[:-1]))


def _check_builtin(rule: str, file_path: str) -> str | None:
    """Return a built-in suppression note if this file matches a known-safe context."""
    suppressions = _BUILT_IN_SUPPRESSIONS.get(rule, [])
    norm = file_path.replace("\\", "/")
    for prefix, note in suppressions:
        if norm.startswith(prefix) or f"/{prefix}/" in norm:
            return note
    return None


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
