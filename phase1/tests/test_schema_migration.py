#!/usr/bin/env python3
r"""Tests for the v1 schema layout and the v0 -> v1 migration.

Every test works on a disposable database inside pytest's own tmp_path (the
"db_path" fixture from conftest.py), so nothing is ever written to the real
/tmp, C:\tmp or the user home directory.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import protocol


# The pre-P1 (v0) schema: role_specs / tasks / audit_log / claims without the
# P1 columns, i.e. exactly the structure that existed before the v1 upgrade.
V0_SCHEMA = """
PRAGMA journal_mode=DELETE;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS role_specs (
    role_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    role_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'BACKLOG',
    acceptance_criteria TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    claimed_by TEXT,
    claimed_at TEXT,
    package_hash TEXT,
    worktree_path TEXT,
    FOREIGN KEY (role_id) REFERENCES role_specs(role_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    evidence TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    package_hash TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    released_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_log(task_id);
"""

V1_TABLES = {
    "role_specs",
    "executors",
    "tasks",
    "task_dependencies",
    "audit_log",
    "task_evidence",
    "claims",
}

V1_INDEXES = {
    "idx_tasks_status",
    "idx_audit_task",
    "idx_deps_task",
    "idx_evidence_task",
}

NEW_TASK_COLUMNS = (
    "priority",
    "dependencies_json",
    "executor_id",
    "implementer_executor_id",
    "retry_count",
    "max_retries",
    "allowed_paths_json",
    "behavioral_check",
    "pass_condition",
    "last_error",
)

ROLE_ID = "A3-backend-engineer"
TASK_ID = "T-old"
STAMP = "2026-01-01T00:00:00+00:00"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn: sqlite3.Connection) -> set:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _create_v0_database(db_path: Path) -> None:
    """Build a v0 database with one role, one task and two audit rows."""
    with closing(_connect(db_path)) as conn:
        conn.executescript(V0_SCHEMA)
        conn.execute(
            "INSERT INTO role_specs (role_id, name, content_hash, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ROLE_ID, ROLE_ID, "hash-abc", "# role spec", STAMP),
        )
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, role_id, status, "
            "acceptance_criteria, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (TASK_ID, "Old task", "old description", ROLE_ID, "BACKLOG",
             "old criteria", "A0", STAMP, STAMP),
        )
        conn.executemany(
            "INSERT INTO audit_log (task_id, actor, from_status, to_status, reason, "
            "evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (TASK_ID, "A0", None, "BACKLOG", "task created", "", STAMP),
                (TASK_ID, "A0", "BACKLOG", "READY", "dependencies satisfied", "", STAMP),
            ],
        )
        conn.commit()


def test_fresh_install_creates_the_full_v1_schema(db_path: Path) -> None:
    protocol.ensure_schema()

    with closing(_connect(db_path)) as conn:
        assert V1_TABLES <= _tables(conn)
        assert set(NEW_TASK_COLUMNS) <= _columns(conn, "tasks")
        assert "evidence_path" in _columns(conn, "audit_log")
        assert "released_at" in _columns(conn, "claims")
        assert V1_INDEXES <= _indexes(conn)

        # Migration reaches the current schema version (follow the constant,
        # do not hard-code a version that drifts on each schema bump).
        assert conn.execute("PRAGMA user_version").fetchone()[0] == protocol.SCHEMA_VERSION

        executors = conn.execute(
            "SELECT executor_id, kind, active FROM executors ORDER BY executor_id"
        ).fetchall()
        assert [r["executor_id"] for r in executors] == [
            "claude", "codex", "cursor", "doubao", "human",
        ]
        assert {r["kind"] for r in executors} == {
            "doubao", "codex", "claude", "cursor", "human",
        }
        assert all(r["active"] == 1 for r in executors)


def test_fresh_install_enforces_the_v1_checks(db_path: Path) -> None:
    """A fresh database carries the library level CHECK constraints."""
    protocol.ensure_schema()

    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO role_specs (role_id, name, content_hash, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (ROLE_ID, ROLE_ID, "h", "c", STAMP),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tasks (task_id, title, role_id, created_by, created_at, "
                "updated_at, priority) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (TASK_ID, "t", ROLE_ID, "A0", STAMP, STAMP, "urgent"),
            )


def test_v0_database_is_migrated_to_v1(db_path: Path) -> None:
    _create_v0_database(db_path)

    protocol.ensure_schema()

    with closing(_connect(db_path)) as conn:
        # the old rows survive with their old values
        role = conn.execute("SELECT * FROM role_specs").fetchone()
        assert role["role_id"] == ROLE_ID
        assert role["content_hash"] == "hash-abc"
        assert role["content"] == "# role spec"

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        task = conn.execute("SELECT * FROM tasks").fetchone()
        assert task["task_id"] == TASK_ID
        assert task["title"] == "Old task"
        assert task["status"] == "BACKLOG"
        assert task["acceptance_criteria"] == "old criteria"

        # the added columns carry their documented defaults
        assert task["priority"] == "medium"
        assert task["dependencies_json"] == "[]"
        assert task["executor_id"] is None
        assert task["implementer_executor_id"] is None
        assert task["retry_count"] == 0
        assert task["max_retries"] == 3
        assert task["allowed_paths_json"] == "[]"
        assert task["behavioral_check"] == ""
        assert task["pass_condition"] == ""
        assert task["last_error"] is None

        audits = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        assert len(audits) == 2
        assert [a["to_status"] for a in audits] == ["BACKLOG", "READY"]
        assert [a["reason"] for a in audits] == [
            "task created", "dependencies satisfied",
        ]
        assert all(a["evidence_path"] is None for a in audits)

        # the new tables and indexes are in place
        assert {"executors", "task_dependencies", "task_evidence"} <= _tables(conn)
        assert V1_INDEXES <= _indexes(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == protocol.SCHEMA_VERSION
        executors = conn.execute(
            "SELECT executor_id, active FROM executors ORDER BY executor_id"
        ).fetchall()
        assert len(executors) == 5
        assert all(r["active"] == 1 for r in executors)


def test_migrated_columns_have_no_library_check(db_path: Path) -> None:
    """Documented trade-off: a column added by ALTER TABLE has no CHECK."""
    _create_v0_database(db_path)

    protocol.ensure_schema()

    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE tasks SET priority='urgent' WHERE task_id=?", (TASK_ID,))
        assert conn.execute(
            "SELECT priority FROM tasks WHERE task_id=?", (TASK_ID,)
        ).fetchone()[0] == "urgent"


def test_ensure_schema_is_idempotent(db_path: Path) -> None:
    protocol.ensure_schema()
    protocol.ensure_schema()
    protocol.ensure_schema()

    with closing(_connect(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == protocol.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM executors").fetchone()[0] == 5
        assert conn.execute(
            "SELECT COUNT(DISTINCT executor_id) FROM executors"
        ).fetchone()[0] == 5


def test_ensure_schema_after_migration_keeps_the_old_data(db_path: Path) -> None:
    _create_v0_database(db_path)

    protocol.ensure_schema()
    protocol.ensure_schema()

    with closing(_connect(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == protocol.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM executors").fetchone()[0] == 5
        task = conn.execute("SELECT * FROM tasks").fetchone()
        assert task["priority"] == "medium"
        assert task["max_retries"] == 3


def test_init_db_delegates_to_ensure_schema(db_path: Path, capsys) -> None:
    protocol.init_db()

    captured = capsys.readouterr()
    assert f"DB initialized at {db_path}" in captured.out

    with closing(_connect(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == protocol.SCHEMA_VERSION
        assert V1_TABLES <= _tables(conn)
        assert conn.execute("SELECT COUNT(*) FROM executors").fetchone()[0] == 5
