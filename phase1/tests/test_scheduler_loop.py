#!/usr/bin/env python3
"""Hermetic tests for the P3 scheduling-loop kernel (no real executors)."""
from __future__ import annotations

import pytest

import orchestrator
import protocol
from scheduler_loop import run_schedule_once

ROLE = "A3-backend-engineer"


def _make_role() -> None:
    orchestrator.ensure_orchestrator()
    with protocol.get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO role_specs (role_id, name, content_hash, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ROLE, ROLE, "hash", "# role spec", protocol.utcnow()))
        conn.commit()


def _make_ready(task_id: str) -> None:
    protocol.create_task(task_id, task_id, "desc", ROLE, "criteria")
    protocol.ready_task(task_id)


def test_schedule_once_processes_one_ready_task(db_path) -> None:
    _make_role()
    _make_ready("T-SCH-1")
    results = run_schedule_once("doubao", "implement",
                                lambda: orchestrator.SimulatedAdapter(),
                                max_tasks=1)
    assert results
    assert results[0]["task_id"] == "T-SCH-1"
    assert results[0]["status"] in ("worked", "idle", "error")
    task = protocol.get_task("T-SCH-1")
    # The simulated implement node advances the task; it must NOT be auto-accepted.
    assert task["status"] not in ("ACCEPTED", "DONE")
    # Worker session is closed after the pass.
    with protocol.get_db() as conn:
        row = conn.execute(
            "SELECT running FROM worker_sessions WHERE executor_id='doubao' AND node='implement'"
        ).fetchone()
    assert row is None or not row["running"]


def test_schedule_once_idles_when_pool_empty(db_path) -> None:
    _make_role()
    results = run_schedule_once("doubao", "implement",
                                lambda: orchestrator.SimulatedAdapter())
    assert results == [{"status": "idle", "task_id": None, "error": None}]


def test_schedule_once_quota_hook_pauses(db_path) -> None:
    _make_role()
    _make_ready("T-SCH-2")
    results = run_schedule_once("doubao", "implement",
                                lambda: orchestrator.SimulatedAdapter(),
                                quota_hook=lambda: "quota exhausted")
    assert results == [{"status": "paused", "task_id": None,
                        "error": "quota exhausted"}]
    # The task is untouched: no session opened, still READY.
    assert protocol.get_task("T-SCH-2")["status"] == "READY"


def test_schedule_once_quota_hook_exception_is_fail_closed(db_path) -> None:
    # R-87: a quota query failure must pause, never flip to fail-open.
    _make_role()
    _make_ready("T-SCH-3")

    def boom():
        raise RuntimeError("db down")

    results = run_schedule_once("doubao", "implement",
                                lambda: orchestrator.SimulatedAdapter(),
                                quota_hook=boom)
    assert results[0]["status"] == "paused"
    assert "fail-closed" in results[0]["error"]
    assert protocol.get_task("T-SCH-3")["status"] == "READY"
