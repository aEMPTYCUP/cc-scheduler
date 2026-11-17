#!/usr/bin/env python3
r"""State-machine tests for the Phase 1 protocol (task T-05).

Covers the three exception states added by P1_SCHEMA_DESIGN.md section 5
(FAILED / NEEDS_HUMAN / BLOCKED) and the audit_log.evidence_path column.

Every test uses the conftest "db_path" fixture, so the protocol DB is
redirected into pytest's own tmp_path -- nothing touches the real /tmp,
C:\tmp or the user home directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import protocol

ROLE_ID = "A3-backend-engineer"
TASK_ID = "T-05-state-machine"

# The acceptance decision must come from an identity other than the
# implementer (T-07 self-acceptance guard in protocol.py).
REVIEWER = "A7-reviewer"

# The edges that P1 section 5 adds on top of the original mapping. The
# original ten edges (CLAIMED->IN_PROGRESS, IN_PROGRESS->SUBMITTED,
# SUBMITTED->REVIEW, REVIEW->ACCEPTED, REVIEW->REWORK, REWORK->CLAIMED,
# REWORK->IN_PROGRESS, ACCEPTED->INTEGRATION, INTEGRATION->REVIEW,
# INTEGRATION->DONE) are kept untouched and regression-tested separately.
NEW_EDGES = [
    ("CLAIMED", "FAILED"),
    ("CLAIMED", "BLOCKED"),
    ("CLAIMED", "NEEDS_HUMAN"),
    ("IN_PROGRESS", "FAILED"),
    ("IN_PROGRESS", "BLOCKED"),
    ("IN_PROGRESS", "NEEDS_HUMAN"),
    ("REVIEW", "NEEDS_HUMAN"),
    ("FAILED", "IN_PROGRESS"),
    ("FAILED", "BLOCKED"),
    ("BLOCKED", "IN_PROGRESS"),
    ("BLOCKED", "NEEDS_HUMAN"),
    ("NEEDS_HUMAN", "IN_PROGRESS"),
    ("NEEDS_HUMAN", "REVIEW"),
    ("NEEDS_HUMAN", "BLOCKED"),
]

# Transitions that must stay illegal: no status change, no audit write.
ILLEGAL_EDGES = [
    ("BACKLOG", "DONE"),
    ("DONE", "IN_PROGRESS"),
    ("FAILED", "REVIEW"),
]

# The original happy-path chain, kept alive by P1.
LEGACY_CHAIN = [
    ("CLAIMED", "IN_PROGRESS"),
    ("IN_PROGRESS", "SUBMITTED"),
    ("SUBMITTED", "REVIEW"),
    ("REVIEW", "ACCEPTED"),
    ("ACCEPTED", "INTEGRATION"),
    ("INTEGRATION", "DONE"),
]

# Shortest legal path from CLAIMED to each status a test may start from.
# BACKLOG/READY are never reached through transition().
_PATHS = {
    "CLAIMED": [],
    "IN_PROGRESS": ["IN_PROGRESS"],
    "REVIEW": ["IN_PROGRESS", "SUBMITTED", "REVIEW"],
    "DONE": ["IN_PROGRESS", "SUBMITTED", "REVIEW", "ACCEPTED", "INTEGRATION", "DONE"],
    "FAILED": ["FAILED"],
    "BLOCKED": ["BLOCKED"],
    "NEEDS_HUMAN": ["NEEDS_HUMAN"],
}


def _create_task(db_path: Path, task_id: str = TASK_ID, claim: bool = True) -> str:
    """Create a task; by default it ends up CLAIMED (BACKLOG -> READY -> CLAIMED)."""
    protocol.init_db()
    protocol.register_role(ROLE_ID)
    protocol.create_task(
        task_id,
        title="State machine test task",
        description="Exercise the phase-1 protocol state machine.",
        role_id=ROLE_ID,
        acceptance_criteria="Every transition lands in the expected status.",
    )
    if claim:
        protocol.ready_task(task_id)
        protocol.atomic_claim(task_id, agent_id=ROLE_ID)
    return task_id


def _drive_to(task_id: str, status: str) -> None:
    """Move an already-claimed task to `status` through legal transitions."""
    for to_status in _PATHS[status]:
        actor = REVIEWER if to_status == "ACCEPTED" else ROLE_ID
        protocol.transition(task_id, to_status, actor=actor, reason=f"drive -> {to_status}")
    assert protocol.get_task(task_id)["status"] == status


@pytest.mark.parametrize(("from_status", "to_status"), NEW_EDGES)
def test_new_edge_is_allowed_and_audited(
    db_path: Path, from_status: str, to_status: str
) -> None:
    """Every edge added in P1 section 5 works and lands in the audit log."""
    _create_task(db_path)
    _drive_to(TASK_ID, from_status)
    audit_before = protocol.get_audit(TASK_ID)

    protocol.transition(
        TASK_ID, to_status, actor=ROLE_ID, reason=f"{from_status} -> {to_status}"
    )

    assert protocol.get_task(TASK_ID)["status"] == to_status

    audit = protocol.get_audit(TASK_ID)
    assert len(audit) == len(audit_before) + 1
    latest = audit[-1]
    assert latest["from_status"] == from_status
    assert latest["to_status"] == to_status
    assert latest["actor"] == ROLE_ID


@pytest.mark.parametrize(("from_status", "to_status"), ILLEGAL_EDGES)
def test_illegal_transition_raises_and_keeps_state(
    db_path: Path, from_status: str, to_status: str
) -> None:
    """An illegal edge raises ValueError and changes neither task nor audit."""
    _create_task(db_path, claim=(from_status != "BACKLOG"))
    if from_status != "BACKLOG":
        _drive_to(TASK_ID, from_status)

    audit_before = protocol.get_audit(TASK_ID)
    assert len(audit_before) > 0

    with pytest.raises(ValueError):
        protocol.transition(TASK_ID, to_status, actor=ROLE_ID, reason="illegal edge")

    assert protocol.get_task(TASK_ID)["status"] == from_status
    assert protocol.get_audit(TASK_ID) == audit_before


def test_legacy_chain_still_reaches_done(db_path: Path) -> None:
    """The original CLAIMED -> ... -> DONE chain is untouched by P1."""
    _create_task(db_path)
    assert protocol.get_task(TASK_ID)["status"] == "CLAIMED"

    for from_status, to_status in LEGACY_CHAIN:
        actor = REVIEWER if to_status == "ACCEPTED" else ROLE_ID
        protocol.transition(
            TASK_ID, to_status, actor=actor, reason=f"{from_status} -> {to_status}"
        )
        assert protocol.get_task(TASK_ID)["status"] == to_status

    audit = protocol.get_audit(TASK_ID)
    assert [row["to_status"] for row in audit] == [
        "BACKLOG",
        "READY",
        "CLAIMED",
        "IN_PROGRESS",
        "SUBMITTED",
        "REVIEW",
        "ACCEPTED",
        "INTEGRATION",
        "DONE",
    ]
    assert audit[-1]["from_status"] == "INTEGRATION"
    assert audit[-1]["to_status"] == "DONE"


def test_evidence_path_is_written_to_audit_log(db_path: Path) -> None:
    """A given evidence_path lands in the newest audit_log row."""
    _create_task(db_path)

    protocol.transition(
        TASK_ID,
        "IN_PROGRESS",
        actor=ROLE_ID,
        reason="with evidence path",
        evidence="see the run log",
        evidence_path="phase1/artifacts/T-05/run.log",
    )

    latest = protocol.get_audit(TASK_ID)[-1]
    assert latest["evidence_path"] == "phase1/artifacts/T-05/run.log"
    assert latest["evidence"] == "see the run log"
    assert latest["to_status"] == "IN_PROGRESS"


def test_evidence_path_is_null_when_not_given(db_path: Path) -> None:
    """Without evidence_path the column is NULL and evidence text is kept."""
    _create_task(db_path)

    protocol.transition(
        TASK_ID,
        "IN_PROGRESS",
        actor=ROLE_ID,
        reason="no evidence path",
        evidence="plain text only",
    )

    latest = protocol.get_audit(TASK_ID)[-1]
    assert latest["evidence_path"] is None
    assert latest["evidence"] == "plain text only"


def test_exception_states_are_reachable_and_escapable(db_path: Path) -> None:
    """FAILED / BLOCKED / NEEDS_HUMAN are reachable and can be left again."""
    _create_task(db_path)
    assert protocol.get_task(TASK_ID)["status"] == "CLAIMED"

    protocol.transition(TASK_ID, "FAILED", actor=ROLE_ID, reason="attempt failed")
    assert protocol.get_task(TASK_ID)["status"] == "FAILED"

    protocol.transition(TASK_ID, "BLOCKED", actor=ROLE_ID, reason="retries exhausted")
    assert protocol.get_task(TASK_ID)["status"] == "BLOCKED"

    protocol.transition(TASK_ID, "NEEDS_HUMAN", actor=ROLE_ID, reason="needs an owner")
    assert protocol.get_task(TASK_ID)["status"] == "NEEDS_HUMAN"

    protocol.transition(TASK_ID, "REVIEW", actor=ROLE_ID, reason="human supplied evidence")
    assert protocol.get_task(TASK_ID)["status"] == "REVIEW"

    audit = protocol.get_audit(TASK_ID)
    assert [row["to_status"] for row in audit][-4:] == [
        "FAILED",
        "BLOCKED",
        "NEEDS_HUMAN",
        "REVIEW",
    ]
