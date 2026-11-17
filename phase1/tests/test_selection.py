#!/usr/bin/env python3
r"""Tests for the structured task contract and the READY-task selection.

Task T-08. Every test uses the conftest "db_path" fixture, so the protocol
DB and the generated task packages are redirected into pytest's own tmp_path
-- nothing touches the real /tmp, C:\tmp or the user home directory.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import protocol

ROLE_ID = "A3-backend-engineer"

# The pre-P1 (v0) tables. A migrated database carries no library level
# CHECK on priority, so the validation has to live in the application.
V0_SCHEMA = """
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
    worktree_path TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    evidence TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    package_hash TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    released_at TEXT
);
"""

STAMP = "2026-01-01T00:00:00+00:00"

# The acceptance decision must come from an identity other than the
# implementer (the T-07 self-acceptance guard in protocol.py).
REVIEWER = "A7-reviewer"


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the test database with the foreign keys the protocol uses."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _setup() -> None:
    """Fresh schema plus the role every task is attached to."""
    protocol.init_db()
    protocol.register_role(ROLE_ID)


def _create(task_id: str, **contract) -> str:
    """Create a task through the legacy call plus optional contract keywords."""
    protocol.create_task(
        task_id,
        title=f"Selection test {task_id}",
        description="Task T-08 structured contract and selection.",
        role_id=ROLE_ID,
        acceptance_criteria="The contract fields survive the round trip.",
        **contract,
    )
    return task_id


def _drive_ready_to_done(task_id: str) -> None:
    """Walk a READY task to DONE through the real state machine."""
    protocol.atomic_claim(task_id, agent_id=ROLE_ID)
    for to_status in ("IN_PROGRESS", "SUBMITTED", "REVIEW"):
        protocol.transition(task_id, to_status, actor=ROLE_ID, reason=f"test -> {to_status}")
    protocol.transition(task_id, "ACCEPTED", actor=REVIEWER, reason="test -> ACCEPTED")
    for to_status in ("INTEGRATION", "DONE"):
        protocol.transition(task_id, to_status, actor=ROLE_ID, reason=f"test -> {to_status}")


def _pin_created_at(db_path: Path, task_id: str, created_at: str) -> None:
    """Pin created_at so the tie-break is deterministic, not clock-dependent."""
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE tasks SET created_at=? WHERE task_id=?", (created_at, task_id))
        conn.commit()


def test_legacy_create_task_signature_keeps_the_defaults(db_path: Path) -> None:
    """The old six-argument call still works and takes the column defaults."""
    _setup()
    protocol.create_task(
        "T-08-legacy",
        title="Legacy six argument call",
        description="Old call sites must keep working unchanged.",
        role_id=ROLE_ID,
        acceptance_criteria="The new columns keep their defaults.",
    )

    task = protocol.get_task("T-08-legacy")
    assert task is not None
    assert task["status"] == "BACKLOG"
    assert task["priority"] == "medium"
    assert task["dependencies_json"] == "[]"
    assert task["allowed_paths_json"] == "[]"
    assert task["behavioral_check"] == ""
    assert task["pass_condition"] == ""
    assert task["max_retries"] == 3

    audit = protocol.get_audit("T-08-legacy")
    assert [row["to_status"] for row in audit] == ["BACKLOG"]
    assert audit[0]["reason"] == "task created"

    with closing(_connect(db_path)) as conn:
        deps = conn.execute(
            "SELECT COUNT(*) FROM task_dependencies WHERE task_id=?", ("T-08-legacy",)
        ).fetchone()[0]
    assert deps == 0


def test_create_task_persists_the_structured_contract(db_path: Path) -> None:
    """Every new field lands in its column, the relations in the join table."""
    _setup()
    _create("T-08-dep-a")
    _create(
        "T-08-dep-b",
        priority="high",
        dependencies=["T-08-dep-a"],
        allowed_paths=["phase1/protocol.py", "phase1/tests/test_selection.py"],
        behavioral_check="pytest phase1/tests -q is green",
        pass_condition="the whole suite passes",
        max_retries=5,
    )

    task = protocol.get_task("T-08-dep-b")
    assert task is not None
    assert task["status"] == "BACKLOG"
    assert task["priority"] == "high"
    assert task["dependencies_json"] == '["T-08-dep-a"]'
    assert json.loads(task["allowed_paths_json"]) == [
        "phase1/protocol.py", "phase1/tests/test_selection.py"
    ]
    assert task["behavioral_check"] == "pytest phase1/tests -q is green"
    assert task["pass_condition"] == "the whole suite passes"
    assert task["max_retries"] == 5

    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT depends_on FROM task_dependencies WHERE task_id=? ORDER BY depends_on",
            ("T-08-dep-b",),
        ).fetchall()
    assert [row["depends_on"] for row in rows] == ["T-08-dep-a"]


def test_dependencies_json_is_sorted_and_deduplicated(db_path: Path) -> None:
    """The snapshot is sorted; a repeated dependency is stored only once."""
    _setup()
    _create("T-08-dep-x")
    _create("T-08-dep-y")
    _create("T-08-dep-z", dependencies=["T-08-dep-y", "T-08-dep-x", "T-08-dep-y"])

    task = protocol.get_task("T-08-dep-z")
    assert task is not None
    assert task["dependencies_json"] == '["T-08-dep-x", "T-08-dep-y"]'

    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT depends_on FROM task_dependencies WHERE task_id=? ORDER BY depends_on",
            ("T-08-dep-z",),
        ).fetchall()
    assert [row["depends_on"] for row in rows] == ["T-08-dep-x", "T-08-dep-y"]


def test_select_next_task_prefers_high_priority(db_path: Path) -> None:
    """high beats medium beats low, whatever the creation order is."""
    _setup()
    for task_id, priority in (
        ("T-08-low", "low"),
        ("T-08-medium", "medium"),
        ("T-08-high", "high"),
    ):
        _create(task_id, priority=priority)
        protocol.ready_task(task_id)

    chosen = protocol.select_next_task()
    assert chosen is not None
    assert chosen["task_id"] == "T-08-high"


def test_select_next_task_prefers_the_oldest_task_of_equal_priority(db_path: Path) -> None:
    """Equal priority falls back to created_at, oldest first."""
    _setup()
    for task_id in ("T-08-newer", "T-08-older"):
        _create(task_id, priority="medium")
        protocol.ready_task(task_id)
    _pin_created_at(db_path, "T-08-newer", "2026-01-02T00:00:00+00:00")
    _pin_created_at(db_path, "T-08-older", "2026-01-01T00:00:00+00:00")

    chosen = protocol.select_next_task()
    assert chosen is not None
    assert chosen["task_id"] == "T-08-older"


def test_select_next_task_breaks_a_created_at_tie_with_the_task_id(db_path: Path) -> None:
    """Identical created_at falls back to the task_id, ascending."""
    _setup()
    for task_id in ("T-08-tie-b", "T-08-tie-a"):
        _create(task_id)
        protocol.ready_task(task_id)
    _pin_created_at(db_path, "T-08-tie-a", "2026-01-01T00:00:00+00:00")
    _pin_created_at(db_path, "T-08-tie-b", "2026-01-01T00:00:00+00:00")

    chosen = protocol.select_next_task()
    assert chosen is not None
    assert chosen["task_id"] == "T-08-tie-a"


def test_select_next_task_waits_for_its_dependencies(db_path: Path) -> None:
    """A high priority task stays blocked until its dependency is DONE."""
    _setup()
    _create("T-08-blocker")
    _create("T-08-blocked", priority="high", dependencies=["T-08-blocker"])
    protocol.ready_task("T-08-blocked")
    protocol.ready_task("T-08-blocker")

    # The dependency is READY but not DONE, so the blocked task is skipped
    # even though it is the high priority one.
    chosen = protocol.select_next_task()
    assert chosen is not None
    assert chosen["task_id"] == "T-08-blocker"

    _drive_ready_to_done("T-08-blocker")

    chosen = protocol.select_next_task()
    assert chosen is not None
    assert chosen["task_id"] == "T-08-blocked"


def test_select_next_task_returns_none_when_the_only_candidate_is_blocked(db_path: Path) -> None:
    """No qualifying task means None, not an error and not a fallback."""
    _setup()
    _create("T-08-prereq")
    _create("T-08-waiting", priority="high", dependencies=["T-08-prereq"])
    protocol.ready_task("T-08-waiting")
    # The prerequisite stays in BACKLOG, so it is not a candidate either.

    assert protocol.select_next_task() is None


def test_select_next_task_ignores_tasks_that_are_not_ready(db_path: Path) -> None:
    """Only READY tasks are candidates; BACKLOG is invisible to the selection."""
    _setup()
    _create("T-08-backlog", priority="high")

    assert protocol.select_next_task() is None


def test_select_next_task_is_read_only(db_path: Path) -> None:
    """Two calls return the same task and change nothing in the database."""
    _setup()
    _create("T-08-readonly", priority="high")
    protocol.ready_task("T-08-readonly")

    with closing(_connect(db_path)) as conn:
        audits_before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    before = protocol.get_task("T-08-readonly")
    assert before is not None

    first = protocol.select_next_task()
    second = protocol.select_next_task()
    assert first is not None
    assert second is not None
    assert first["task_id"] == second["task_id"] == "T-08-readonly"

    task = protocol.get_task("T-08-readonly")
    assert task is not None
    assert task["status"] == "READY"
    assert task["claimed_by"] is None
    assert task["updated_at"] == before["updated_at"]
    assert task["created_at"] == before["created_at"]

    with closing(_connect(db_path)) as conn:
        audits_after = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    assert audits_after == audits_before
    assert claims == 0


@pytest.mark.parametrize("bad_priority", ["urgent", "HIGH", "", None])
def test_create_task_rejects_an_invalid_priority(db_path: Path, bad_priority) -> None:
    """The application validates the priority itself, not the database."""
    _setup()
    with pytest.raises(ValueError):
        _create("T-08-bad-priority", priority=bad_priority)
    assert protocol.get_task("T-08-bad-priority") is None


@pytest.mark.parametrize("bad_retries", [-1, -10, 1.5, "3"])
def test_create_task_rejects_a_bad_max_retries(db_path: Path, bad_retries) -> None:
    """max_retries must be a non-negative integer."""
    _setup()
    with pytest.raises(ValueError):
        _create("T-08-bad-retries", max_retries=bad_retries)
    assert protocol.get_task("T-08-bad-retries") is None


def test_create_task_rejects_a_missing_dependency(db_path: Path) -> None:
    """A dependency that does not exist is refused and named in the error."""
    _setup()
    with pytest.raises(ValueError) as excinfo:
        _create("T-08-missing-dep", dependencies=["T-08-does-not-exist"])
    assert "T-08-does-not-exist" in str(excinfo.value)
    assert protocol.get_task("T-08-missing-dep") is None


def test_create_task_names_every_missing_dependency(db_path: Path) -> None:
    """All unknown ids are reported, not only the first one."""
    _setup()
    with pytest.raises(ValueError) as excinfo:
        _create("T-08-many-missing", dependencies=["T-08-gone-a", "T-08-gone-b"])
    message = str(excinfo.value)
    assert "T-08-gone-a" in message
    assert "T-08-gone-b" in message
    assert protocol.get_task("T-08-many-missing") is None


def test_migrated_database_still_rejects_an_invalid_priority(db_path: Path) -> None:
    """A migrated database has no CHECK on priority, so the app must refuse it."""
    with closing(_connect(db_path)) as conn:
        conn.executescript(V0_SCHEMA)
        conn.execute(
            "INSERT INTO tasks (task_id, title, role_id, status, created_by, "
            "created_at, updated_at) VALUES (?, ?, ?, 'BACKLOG', 'A0', ?, ?)",
            ("T-08-legacy-row", "legacy row", ROLE_ID, STAMP, STAMP),
        )
        conn.commit()

    protocol.ensure_schema()
    protocol.register_role(ROLE_ID)

    # The migrated column really has no library level CHECK.
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE tasks SET priority='urgent' WHERE task_id=?", ("T-08-legacy-row",)
        )
        conn.commit()
        stored = conn.execute(
            "SELECT priority FROM tasks WHERE task_id=?", ("T-08-legacy-row",)
        ).fetchone()[0]
    assert stored == "urgent"

    with pytest.raises(ValueError):
        _create("T-08-migrated-bad", priority="urgent")
    assert protocol.get_task("T-08-migrated-bad") is None
