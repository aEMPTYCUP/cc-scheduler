#!/usr/bin/env python3
r"""Smoke test for the Phase 1 protocol happy path.

Drives one task from BACKLOG all the way to DONE through the real protocol
API and checks that the audit log tells the whole story.
"""

from __future__ import annotations

from pathlib import Path

import protocol

ROLE_ID = "A3-backend-engineer"
TASK_ID = "T-03-smoke"

# BACKLOG/READY/CLAIMED are reached by create_task, ready_task and
# atomic_claim; the rest are explicit transitions.
EXPECTED_STATUS_CHAIN = [
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


def test_task_reaches_done_with_full_audit_chain(db_path: Path) -> None:
    protocol.init_db()
    assert db_path.exists()

    protocol.register_role(ROLE_ID)
    protocol.create_task(
        TASK_ID,
        title="Smoke test: protocol happy path",
        description="Drive one task from BACKLOG to DONE.",
        role_id=ROLE_ID,
        acceptance_criteria="Task status is DONE and the audit chain is complete.",
    )
    protocol.ready_task(TASK_ID)
    protocol.atomic_claim(TASK_ID, agent_id=ROLE_ID)

    for to_status in EXPECTED_STATUS_CHAIN[3:]:
        protocol.transition(
            TASK_ID, to_status, actor=ROLE_ID, reason=f"smoke -> {to_status}"
        )

    task = protocol.get_task(TASK_ID)
    assert task is not None
    assert task["status"] == "DONE"

    audit = protocol.get_audit(TASK_ID)
    assert [row["to_status"] for row in audit] == EXPECTED_STATUS_CHAIN
    assert audit[0]["from_status"] is None
    assert audit[0]["reason"] == "task created"
    assert audit[-1]["from_status"] == "INTEGRATION"
    assert audit[-1]["to_status"] == "DONE"
