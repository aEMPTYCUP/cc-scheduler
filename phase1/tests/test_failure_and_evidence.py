#!/usr/bin/env python3
r"""Tests for the failure handling and the evidence helpers (task T-09).

Covers record_failure (retry counter, last_error, FAILED vs BLOCKED),
block_task, request_human, release_claim and add_evidence.

Every test uses the conftest "db_path" fixture, so the protocol DB is
redirected into pytest's own tmp_path -- nothing touches the real /tmp,
C:\tmp or the user home directory.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import protocol

ROLE_ID = "A3-backend-engineer"

# The acceptance decision must come from an identity other than the
# implementer (T-07 self-acceptance guard in protocol.py).
REVIEWER = "A7-reviewer"

TASK_ID = "T-09-failure-and-evidence"

# Shortest legal path from CLAIMED to each status a test starts from.
_PATHS = {
    "CLAIMED": [],
    "IN_PROGRESS": ["IN_PROGRESS"],
    "SUBMITTED": ["IN_PROGRESS", "SUBMITTED"],
    "REVIEW": ["IN_PROGRESS", "SUBMITTED", "REVIEW"],
    "DONE": ["IN_PROGRESS", "SUBMITTED", "REVIEW", "ACCEPTED", "INTEGRATION", "DONE"],
    "FAILED": ["FAILED"],
    "BLOCKED": ["BLOCKED"],
    "NEEDS_HUMAN": ["NEEDS_HUMAN"],
}


def _create_task(db_path: Path, task_id: str = TASK_ID, max_retries: int = 3) -> str:
    """Create a task and claim it (BACKLOG -> READY -> CLAIMED)."""
    protocol.init_db()
    protocol.register_role(ROLE_ID)
    protocol.create_task(
        task_id,
        title="Failure handling test task",
        description="Exercise record_failure and the evidence helpers.",
        role_id=ROLE_ID,
        acceptance_criteria="The failure path lands in the expected status.",
        max_retries=max_retries,
    )
    protocol.ready_task(task_id)
    protocol.atomic_claim(task_id, agent_id=ROLE_ID)
    return task_id


def _drive_to(task_id: str, status: str) -> None:
    """Move an already claimed task to `status` through legal transitions."""
    for to_status in _PATHS[status]:
        actor = REVIEWER if to_status == "ACCEPTED" else ROLE_ID
        protocol.transition(task_id, to_status, actor=actor, reason=f"drive -> {to_status}")
    assert protocol.get_task(task_id)["status"] == status


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the test database with the foreign keys the protocol uses."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_record_failure_counts_and_lands_in_failed(db_path: Path) -> None:
    """Under the limit the counter grows and the task sits in FAILED."""
    _create_task(db_path)
    _drive_to(TASK_ID, "IN_PROGRESS")

    protocol.record_failure(TASK_ID, actor=ROLE_ID, error="pytest exited with code 1")

    task = protocol.get_task(TASK_ID)
    assert task["status"] == "FAILED"
    assert task["retry_count"] == 1
    assert task["last_error"] == "pytest exited with code 1"

    # FAILED -> IN_PROGRESS is a legal edge, so the counter keeps running.
    _drive_to(TASK_ID, "IN_PROGRESS")
    protocol.record_failure(TASK_ID, actor=ROLE_ID, error="second attempt failed")

    task = protocol.get_task(TASK_ID)
    assert task["status"] == "FAILED"
    assert task["retry_count"] == 2
    assert task["last_error"] == "second attempt failed"

    # The audit trail names the attempt number and the error.
    reasons = [row["reason"] for row in protocol.get_audit(TASK_ID)]
    assert "failure 1/3: pytest exited with code 1" in reasons
    assert "failure 2/3: second attempt failed" in reasons


def test_record_failure_blocks_when_the_retries_are_exhausted(db_path: Path) -> None:
    """The attempt that reaches max_retries lands in BLOCKED."""
    _create_task(db_path, max_retries=3)
    _drive_to(TASK_ID, "IN_PROGRESS")

    for attempt in (1, 2):
        protocol.record_failure(TASK_ID, actor=ROLE_ID, error=f"attempt {attempt}")
        assert protocol.get_task(TASK_ID)["status"] == "FAILED"
        _drive_to(TASK_ID, "IN_PROGRESS")

    protocol.record_failure(TASK_ID, actor=ROLE_ID, error="attempt 3")

    task = protocol.get_task(TASK_ID)
    assert task["status"] == "BLOCKED"
    assert task["retry_count"] == 3
    assert task["last_error"] == "attempt 3"

    reasons = [row["reason"] for row in protocol.get_audit(TASK_ID)]
    assert "failure 3/3: attempt 3" in reasons


@pytest.mark.parametrize("status", ["REVIEW", "DONE"])
def test_record_failure_rejects_a_task_that_is_not_running(
    db_path: Path, status: str
) -> None:
    """A task that is not running cannot fail: ValueError, nothing changes."""
    _create_task(db_path)
    _drive_to(TASK_ID, status)
    audit_before = protocol.get_audit(TASK_ID)

    with pytest.raises(ValueError):
        protocol.record_failure(TASK_ID, actor=ROLE_ID, error="must not be counted")

    task = protocol.get_task(TASK_ID)
    assert task["status"] == status
    assert task["retry_count"] == 0
    assert task["last_error"] is None
    assert protocol.get_audit(TASK_ID) == audit_before


def test_block_task_from_a_running_task_is_audited(db_path: Path) -> None:
    """block_task moves a running task to BLOCKED and writes the audit row."""
    _create_task(db_path)
    _drive_to(TASK_ID, "IN_PROGRESS")
    audit_before = len(protocol.get_audit(TASK_ID))

    protocol.block_task(TASK_ID, actor=ROLE_ID, reason="waiting for the vendor API key")

    assert protocol.get_task(TASK_ID)["status"] == "BLOCKED"

    latest = protocol.get_audit(TASK_ID)[-1]
    assert latest["from_status"] == "IN_PROGRESS"
    assert latest["to_status"] == "BLOCKED"
    assert latest["actor"] == ROLE_ID
    assert latest["reason"] == "waiting for the vendor API key"
    assert len(protocol.get_audit(TASK_ID)) == audit_before + 1


def test_block_task_rejects_an_illegal_source(db_path: Path) -> None:
    """A submitted task cannot be blocked: ValueError, nothing changes."""
    _create_task(db_path)
    _drive_to(TASK_ID, "SUBMITTED")
    audit_before = protocol.get_audit(TASK_ID)

    with pytest.raises(ValueError):
        protocol.block_task(TASK_ID, actor=ROLE_ID, reason="illegal source")

    assert protocol.get_task(TASK_ID)["status"] == "SUBMITTED"
    assert protocol.get_audit(TASK_ID) == audit_before


def test_request_human_from_review_is_audited(db_path: Path) -> None:
    """request_human moves a task in review to NEEDS_HUMAN and audits it."""
    _create_task(db_path)
    _drive_to(TASK_ID, "REVIEW")
    audit_before = len(protocol.get_audit(TASK_ID))

    protocol.request_human(TASK_ID, actor=ROLE_ID, reason="the evidence is ambiguous")

    assert protocol.get_task(TASK_ID)["status"] == "NEEDS_HUMAN"

    latest = protocol.get_audit(TASK_ID)[-1]
    assert latest["from_status"] == "REVIEW"
    assert latest["to_status"] == "NEEDS_HUMAN"
    assert latest["actor"] == ROLE_ID
    assert latest["reason"] == "the evidence is ambiguous"
    assert len(protocol.get_audit(TASK_ID)) == audit_before + 1


def test_request_human_rejects_an_illegal_source(db_path: Path) -> None:
    """A finished task cannot ask for a human: ValueError, nothing changes."""
    _create_task(db_path)
    _drive_to(TASK_ID, "DONE")
    audit_before = protocol.get_audit(TASK_ID)

    with pytest.raises(ValueError):
        protocol.request_human(TASK_ID, actor=ROLE_ID, reason="illegal source")

    assert protocol.get_task(TASK_ID)["status"] == "DONE"
    assert protocol.get_audit(TASK_ID) == audit_before


def test_release_claim_stamps_the_open_claim(db_path: Path) -> None:
    """The open claim gets a released_at and the task status stays put."""
    _create_task(db_path)
    assert protocol.get_task(TASK_ID)["status"] == "CLAIMED"
    audit_before = len(protocol.get_audit(TASK_ID))

    assert protocol.release_claim(TASK_ID, actor=ROLE_ID) is True

    with closing(_connect(db_path)) as conn:
        claims = conn.execute(
            "SELECT released_at FROM claims WHERE task_id=? ORDER BY id", (TASK_ID,)
        ).fetchall()
    assert len(claims) == 1
    assert claims[0]["released_at"] is not None

    # The state machine has no CLAIMED -> READY edge, so the status must not move.
    assert protocol.get_task(TASK_ID)["status"] == "CLAIMED"

    latest = protocol.get_audit(TASK_ID)[-1]
    assert latest["from_status"] == "CLAIMED"
    assert latest["to_status"] == "CLAIMED"
    assert "released" in latest["reason"]
    assert len(protocol.get_audit(TASK_ID)) == audit_before + 1


def test_release_claim_without_an_open_claim_is_a_no_op(db_path: Path) -> None:
    """A second release finds no open claim: it returns False and writes nothing."""
    _create_task(db_path)
    assert protocol.release_claim(TASK_ID, actor=ROLE_ID) is True
    audit_before = len(protocol.get_audit(TASK_ID))

    assert protocol.release_claim(TASK_ID, actor=ROLE_ID) is False

    assert len(protocol.get_audit(TASK_ID)) == audit_before


def test_add_evidence_stores_the_row_and_audits_it(db_path: Path) -> None:
    """A valid kind lands in task_evidence and in the newest audit row."""
    _create_task(db_path)
    path = "phase1/artifacts/T-09/run.log"

    evidence_id = protocol.add_evidence(
        TASK_ID,
        kind="cli_log",
        path=path,
        created_by=ROLE_ID,
        sha256="a" * 64,
    )

    assert isinstance(evidence_id, int)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM task_evidence WHERE task_id=? ORDER BY id", (TASK_ID,)
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == evidence_id
    assert row["task_id"] == TASK_ID
    assert row["kind"] == "cli_log"
    assert row["path"] == path
    assert row["sha256"] == "a" * 64
    assert row["created_by"] == ROLE_ID
    assert row["created_at"]

    latest = protocol.get_audit(TASK_ID)[-1]
    assert latest["from_status"] == latest["to_status"] == "CLAIMED"
    assert latest["reason"] == "evidence:cli_log"
    assert latest["evidence_path"] == path


def test_add_evidence_rejects_an_unknown_kind(db_path: Path) -> None:
    """A kind outside the allowed set raises ValueError and stores nothing."""
    _create_task(db_path)
    with closing(_connect(db_path)) as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM task_evidence WHERE task_id=?", (TASK_ID,)
        ).fetchone()[0]

    with pytest.raises(ValueError):
        protocol.add_evidence(
            TASK_ID, kind="screenshot", path="shot.png", created_by=ROLE_ID
        )

    with closing(_connect(db_path)) as conn:
        after = conn.execute(
            "SELECT COUNT(*) FROM task_evidence WHERE task_id=?", (TASK_ID,)
        ).fetchone()[0]
    assert after == before


def test_add_evidence_can_skip_the_audit(db_path: Path) -> None:
    """With sync_audit=False the row is stored and no audit row is added."""
    _create_task(db_path)
    audit_before = len(protocol.get_audit(TASK_ID))

    protocol.add_evidence(
        TASK_ID,
        kind="commit_sha",
        path="abc123",
        created_by=ROLE_ID,
        sync_audit=False,
    )

    assert len(protocol.get_audit(TASK_ID)) == audit_before
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM task_evidence WHERE task_id=?", (TASK_ID,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "commit_sha"
    assert rows[0]["sha256"] is None


def test_existing_audit_rows_keep_their_shape(db_path: Path) -> None:
    """The new helpers reuse _audit; the older audit rows stay untouched."""
    _create_task(db_path)
    _drive_to(TASK_ID, "IN_PROGRESS")
    audit_before = protocol.get_audit(TASK_ID)

    protocol.release_claim(TASK_ID, actor=ROLE_ID)

    audit_after = protocol.get_audit(TASK_ID)
    assert audit_after[: len(audit_before)] == audit_before
    assert len(audit_after) == len(audit_before) + 1
