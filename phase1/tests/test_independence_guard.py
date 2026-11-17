#!/usr/bin/env python3
r"""Tests for the self-acceptance guard (task T-07).

The guard rejects a REVIEW -> ACCEPTED or REVIEW -> REWORK decision when the
reviewer identity is the same string as the implementer identity:

  * implementer = tasks.implementer_executor_id (frozen at claim time) or,
    for a legacy claim without an executor id, tasks.claimed_by;
  * reviewer   = the caller's actor_executor_id or, when it is not given,
    the caller's actor.

A refusal leaves the task in REVIEW, writes one rejection audit row
(from_status = to_status = REVIEW) and raises PermissionError.

Limitation, stated on purpose in every docstring that touches the guard:
this only stops the same identity string from accepting its own work. The
A7 role spec demands an independent session that does not resume the
implementer's conversation; that cannot be proven by the state machine and
stays a matter of role discipline plus the audit trail.

Every test uses the conftest "db_path" fixture, so the protocol DB and the
generated task packages are redirected into pytest's own tmp_path --
nothing touches the real /tmp, C:\tmp or the user home directory.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import protocol

ROLE_ID = "A3-backend-engineer"
TASK_ID = "T-07-independence-guard"

# Reviewer identities used by the tests.
REVIEWER = "A7-code-reviewer"


def _create_task(db_path: Path, task_id: str = TASK_ID) -> str:
    """Create a task that sits in READY, waiting to be claimed."""
    assert protocol.DB_PATH == db_path
    protocol.init_db()
    protocol.register_role(ROLE_ID)
    protocol.create_task(
        task_id,
        title="Independence guard test task",
        description="Exercise the self-acceptance guard.",
        role_id=ROLE_ID,
        acceptance_criteria="The implementer cannot accept the task.",
    )
    protocol.ready_task(task_id)
    return task_id


def _drive_to_review(task_id: str, actor: str = ROLE_ID) -> None:
    """Move a claimed task to REVIEW over legal, non-acceptance edges."""
    protocol.transition(task_id, "IN_PROGRESS", actor=actor, reason="work started")
    protocol.transition(task_id, "SUBMITTED", actor=actor, reason="work submitted")
    protocol.transition(task_id, "REVIEW", actor=actor, reason="handed to review")
    assert protocol.get_task(task_id)["status"] == "REVIEW"


def test_implementer_cannot_accept_its_own_task(db_path: Path) -> None:
    """The claimer that also reviews is refused, and the refusal is audited.

    The task stays in REVIEW, exactly one rejection audit row is added and
    the caller receives a PermissionError.
    """
    _create_task(db_path)
    protocol.atomic_claim(TASK_ID, agent_id=ROLE_ID, executor_id="codex")
    _drive_to_review(TASK_ID)
    audit_before = protocol.get_audit(TASK_ID)

    with pytest.raises(PermissionError) as excinfo:
        protocol.transition(
            TASK_ID,
            "ACCEPTED",
            actor=ROLE_ID,
            reason="accept my own work",
            actor_executor_id="codex",
        )

    assert "codex" in str(excinfo.value)
    assert protocol.get_task(TASK_ID)["status"] == "REVIEW"

    audit = protocol.get_audit(TASK_ID)
    assert len(audit) == len(audit_before) + 1
    rejection = audit[-1]
    assert rejection["from_status"] == "REVIEW"
    assert rejection["to_status"] == "REVIEW"
    assert "rejected" in rejection["reason"]
    assert "codex" in rejection["reason"]
    assert rejection["evidence"] == "attempted=ACCEPTED"


def test_legacy_claim_blocks_the_same_actor_only(db_path: Path) -> None:
    """Without an executor id the free-text claimed_by is the implementer.

    "alice" may not accept the task she claimed, "bob" may. Only the
    string identity is compared -- no authentication is involved.
    """
    _create_task(db_path)
    protocol.atomic_claim(TASK_ID, agent_id="alice")
    _drive_to_review(TASK_ID, actor="alice")

    with pytest.raises(PermissionError):
        protocol.transition(TASK_ID, "ACCEPTED", actor="alice", reason="self review")

    assert protocol.get_task(TASK_ID)["status"] == "REVIEW"

    protocol.transition(TASK_ID, "ACCEPTED", actor="bob", reason="independent review")
    assert protocol.get_task(TASK_ID)["status"] == "ACCEPTED"


def test_a_different_executor_may_accept(db_path: Path) -> None:
    """A reviewer running on another executor passes the guard."""
    _create_task(db_path)
    protocol.atomic_claim(TASK_ID, agent_id=ROLE_ID, executor_id="codex")
    _drive_to_review(TASK_ID)

    protocol.transition(
        TASK_ID,
        "ACCEPTED",
        actor=REVIEWER,
        reason="independent review",
        actor_executor_id="claude",
    )

    assert protocol.get_task(TASK_ID)["status"] == "ACCEPTED"


def test_rework_is_guarded_as_well(db_path: Path) -> None:
    """The implementer may not send its own task back to REWORK either."""
    _create_task(db_path)
    protocol.atomic_claim(TASK_ID, agent_id=ROLE_ID, executor_id="codex")
    _drive_to_review(TASK_ID)
    audit_before = protocol.get_audit(TASK_ID)

    with pytest.raises(PermissionError):
        protocol.transition(
            TASK_ID,
            "REWORK",
            actor=ROLE_ID,
            reason="fix it myself",
            actor_executor_id="codex",
        )

    assert protocol.get_task(TASK_ID)["status"] == "REVIEW"

    audit = protocol.get_audit(TASK_ID)
    assert len(audit) == len(audit_before) + 1
    assert audit[-1]["from_status"] == "REVIEW"
    assert audit[-1]["to_status"] == "REVIEW"
    assert "rejected" in audit[-1]["reason"]
    assert audit[-1]["evidence"] == "attempted=REWORK"


def test_non_acceptance_transitions_are_not_guarded(db_path: Path) -> None:
    """The guard only covers the acceptance decisions of a REVIEW task.

    IN_PROGRESS -> SUBMITTED, SUBMITTED -> REVIEW and REVIEW -> NEEDS_HUMAN
    stay untouched, even when the actor is the implementer.
    """
    _create_task(db_path)
    protocol.atomic_claim(TASK_ID, agent_id=ROLE_ID, executor_id="codex")

    protocol.transition(
        TASK_ID, "IN_PROGRESS", actor=ROLE_ID, reason="started", actor_executor_id="codex"
    )
    assert protocol.get_task(TASK_ID)["status"] == "IN_PROGRESS"

    protocol.transition(
        TASK_ID, "SUBMITTED", actor=ROLE_ID, reason="submitted", actor_executor_id="codex"
    )
    protocol.transition(
        TASK_ID, "REVIEW", actor=ROLE_ID, reason="to review", actor_executor_id="codex"
    )
    assert protocol.get_task(TASK_ID)["status"] == "REVIEW"

    protocol.transition(
        TASK_ID, "NEEDS_HUMAN", actor=ROLE_ID, reason="needs a human", actor_executor_id="codex"
    )
    assert protocol.get_task(TASK_ID)["status"] == "NEEDS_HUMAN"


def test_transition_signature_stays_backward_compatible(db_path: Path) -> None:
    """The new keyword is optional and last, so old calls keep working."""
    signature = inspect.signature(protocol.transition)
    parameter = signature.parameters["actor_executor_id"]
    assert parameter.default is None
    assert list(signature.parameters)[-1] == "actor_executor_id"


def test_happy_path_reaches_done_with_an_independent_reviewer(db_path: Path) -> None:
    """BACKLOG -> ... -> DONE still works once the reviewer differs.

    This is the workflow the guard asks for: the implementer submits, an
    independent reviewer accepts. The guard cannot prove the reviewer ran
    in a fresh session -- that remains a role rule, not a state machine
    fact.
    """
    _create_task(db_path)
    protocol.atomic_claim(TASK_ID, agent_id=ROLE_ID, executor_id="codex")
    _drive_to_review(TASK_ID)

    protocol.transition(
        TASK_ID, "ACCEPTED", actor=REVIEWER, reason="ok", actor_executor_id="claude"
    )
    protocol.transition(TASK_ID, "INTEGRATION", actor="A8-integration-release", reason="merged")
    protocol.transition(TASK_ID, "DONE", actor="A8-integration-release", reason="released")

    assert protocol.get_task(TASK_ID)["status"] == "DONE"

    assert [row["to_status"] for row in protocol.get_audit(TASK_ID)] == [
        "BACKLOG", "READY", "CLAIMED", "IN_PROGRESS", "SUBMITTED", "REVIEW",
        "ACCEPTED", "INTEGRATION", "DONE",
    ]
