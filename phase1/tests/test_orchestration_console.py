#!/usr/bin/env python3
r"""Tests for the v2 orchestration console (capabilities, GO sessions, loops).

Every test uses the conftest ``db_path`` fixture, so protocol.DB_PATH and the
task-package directory are redirected into pytest's tmp_path. The orchestrator
only adds a control layer on top of the protocol kernel; all state changes still
flow through atomic_claim/transition, which the existing suite already covers.
"""

from __future__ import annotations

import pytest

import protocol
import orchestrator

ROLE = "A3-backend-engineer"


def _make_role() -> None:
    orchestrator.ensure_orchestrator()  # idempotent schema + seeds
    with protocol.get_db() as conn:
        conn.execute(
            "INSERT INTO role_specs (role_id, name, content_hash, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ROLE, ROLE, "hash", "# role spec", protocol.utcnow()),
        )
        conn.commit()


def _make_ready(task_id: str, priority: str = "medium") -> None:
    protocol.create_task(task_id, task_id, "desc", ROLE, "criteria", priority=priority)
    protocol.ready_task(task_id, actor="test")


def _drive_to_review(task_id: str, implementer: str) -> None:
    """Create, claim as `implementer` and move a task all the way to REVIEW."""
    protocol.create_task(task_id, task_id, "desc", ROLE, "criteria")
    protocol.ready_task(task_id, actor="test")
    protocol.atomic_claim(task_id, agent_id=implementer, executor_id=implementer)
    protocol.transition(task_id, "IN_PROGRESS", implementer, actor_executor_id=implementer)
    protocol.transition(task_id, "SUBMITTED", implementer, actor_executor_id=implementer)
    protocol.transition(task_id, "REVIEW", implementer, actor_executor_id=implementer)


# --------------------------------------------------------------------------- #
# capability switches
# --------------------------------------------------------------------------- #

def test_ensure_seeds_capabilities_and_sessions(db_path) -> None:
    orchestrator.ensure_orchestrator()
    caps = {(c["executor_id"], c["node"]): c for c in orchestrator.list_capabilities()}
    # Doubao is coding-only: implement on, acceptance present but locked off.
    assert caps[("doubao", "implement")]["enabled"] == 1
    assert caps[("doubao", "acceptance")]["enabled"] == 0
    assert caps[("doubao", "acceptance")]["locked"] == 1
    # Claude is the independent auditor.
    assert caps[("claude", "acceptance")]["enabled"] == 1
    sessions = orchestrator.list_sessions()
    assert len(sessions) == 5 * len(orchestrator.NODES)
    assert all(s["running"] == 0 for s in sessions)


def test_doubao_acceptance_cannot_be_enabled_but_codex_can(db_path) -> None:
    orchestrator.ensure_orchestrator()
    with pytest.raises(orchestrator.CapabilityLocked):
        orchestrator.set_capability("doubao", "acceptance", True)
    result = orchestrator.set_capability("codex", "acceptance", True)
    assert result["changed"] is True
    caps = {(c["executor_id"], c["node"]): c for c in orchestrator.list_capabilities()}
    assert caps[("codex", "acceptance")]["enabled"] == 1


def test_disabling_a_switch_stops_a_running_session(db_path) -> None:
    orchestrator.ensure_orchestrator()
    orchestrator.start_worker("doubao", "implement")
    orchestrator.set_capability("doubao", "implement", False)
    session = next(
        s for s in orchestrator.list_sessions()
        if s["executor_id"] == "doubao" and s["node"] == "implement"
    )
    assert session["running"] == 0


# --------------------------------------------------------------------------- #
# GO / STOP guards
# --------------------------------------------------------------------------- #

def test_start_worker_requires_an_enabled_capability(db_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    # codex acceptance defaults to off -> GO refused
    with pytest.raises(orchestrator.WorkerError):
        orchestrator.start_worker("codex", "acceptance")
    # doubao implement defaults to on -> GO accepted
    orchestrator.start_worker("doubao", "implement")
    session = next(
        s for s in orchestrator.list_sessions()
        if s["executor_id"] == "doubao" and s["node"] == "implement"
    )
    assert session["running"] == 1


def test_stop_is_respected_and_leaves_the_pool_untouched(db_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    _make_ready("T1")
    orchestrator.start_worker("doubao", "implement")
    orchestrator.stop_worker("doubao", "implement")
    result = orchestrator.run_worker_once("doubao", "implement")
    assert result["status"] == "stopped"
    assert protocol.get_task("T1")["status"] == "READY"


# --------------------------------------------------------------------------- #
# node-aware selection
# --------------------------------------------------------------------------- #

def test_implement_selection_orders_by_priority_then_creation(db_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    _make_ready("T-low", "low")
    _make_ready("T-high", "high")
    _make_ready("T-med", "medium")
    chosen = orchestrator.select_next_task_for_node("implement", "doubao")
    assert chosen["task_id"] == "T-high"


def test_acceptance_never_offers_a_reviewer_their_own_work(db_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    _drive_to_review("T-own", "claude")
    # Claude implemented T-own, so it must not be offered to Claude for audit.
    assert orchestrator.select_next_task_for_node("acceptance", "claude") is None
    # A task implemented by Doubao is eligible for the independent Claude audit.
    _drive_to_review("T-other", "doubao")
    chosen = orchestrator.select_next_task_for_node("acceptance", "claude")
    assert chosen["task_id"] == "T-other"


def test_disabled_capability_yields_no_task(db_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    _make_ready("T1")
    # cursor implement is enabled by default; turn it off and confirm no offer.
    orchestrator.set_capability("cursor", "implement", False)
    assert orchestrator.select_next_task_for_node("implement", "cursor") is None


# --------------------------------------------------------------------------- #
# worker loops
# --------------------------------------------------------------------------- #

def test_implement_worker_drains_ready_pool_into_review(db_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    for index in range(3):
        _make_ready(f"T{index:02d}")
    orchestrator.start_worker("doubao", "implement")
    results = orchestrator.run_worker_drain("doubao", "implement")
    assert [r["status"] for r in results[:3]] == ["worked", "worked", "worked"]
    assert results[-1]["status"] == "idle"
    statuses = {t["task_id"]: t["status"] for t in protocol.list_tasks()}
    assert set(statuses.values()) == {"REVIEW"}
    # Doubao's implementer identity is frozen on every claimed task.
    assert all(t["implementer_executor_id"] == "doubao" for t in protocol.list_tasks())
    session = next(
        s for s in orchestrator.list_sessions()
        if s["executor_id"] == "doubao" and s["node"] == "implement"
    )
    assert session["current_task_id"] is None


def test_acceptance_worker_moves_review_to_accepted(db_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    _drive_to_review("T1", "doubao")
    orchestrator.start_worker("claude", "acceptance")
    results = orchestrator.run_worker_drain("claude", "acceptance")
    assert results[0]["status"] == "worked"
    assert protocol.get_task("T1")["status"] == "ACCEPTED"


def test_simulated_rework_verdict(db_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    _drive_to_review("T1", "doubao")
    orchestrator.start_worker("claude", "acceptance")
    result = orchestrator.run_worker_once(
        "claude", "acceptance", orchestrator.SimulatedAdapter("REWORK")
    )
    assert result["status"] == "worked"
    assert protocol.get_task("T1")["status"] == "REWORK"


def test_kernel_self_acceptance_guard_fires_through_adapter(db_path) -> None:
    """Selection already hides own work; bypassing it still hits the kernel guard."""
    _make_role()
    orchestrator.ensure_orchestrator()
    _drive_to_review("T1", "claude")
    task = protocol.get_task("T1")
    with pytest.raises(PermissionError):
        orchestrator.SimulatedAdapter().accept(task, "claude")


def test_worker_loop_contains_a_poison_task_and_stops(db_path) -> None:
    """An adapter raising must not crash the loop or fake a result."""
    _make_role()
    orchestrator.ensure_orchestrator()
    _drive_to_review("T1", "doubao")  # eligible for the independent Claude audit
    orchestrator.start_worker("claude", "acceptance")

    class _BoomAdapter(orchestrator.WorkerAdapter):
        def implement(self, task, executor_id: str) -> str:
            raise RuntimeError("boom implement")

        def accept(self, task, executor_id: str) -> str:
            raise RuntimeError("boom accept")

    result = orchestrator.run_worker_once("claude", "acceptance", _BoomAdapter())
    assert result["status"] == "error"
    assert "boom accept" in result["error"]
    # The task is untouched and the session stops instead of spinning on it.
    assert protocol.get_task("T1")["status"] == "REVIEW"
    session = next(
        s for s in orchestrator.list_sessions()
        if s["executor_id"] == "claude" and s["node"] == "acceptance"
    )
    assert session["running"] == 0


# --------------------------------------------------------------------------- #
# human integration, demo pool, console snapshot
# --------------------------------------------------------------------------- #

def test_integrate_task_runs_accepted_to_done(db_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    _drive_to_review("T1", "doubao")
    orchestrator.start_worker("claude", "acceptance")
    orchestrator.run_worker_drain("claude", "acceptance")
    integrated = orchestrator.integrate_task("T1")
    assert integrated["status"] == "DONE"
    with pytest.raises(ValueError):
        orchestrator.integrate_task("missing-id")


def test_seed_demo_pool_is_reproducible_and_stops_workers(db_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    orchestrator.start_worker("doubao", "implement")
    counts = orchestrator.seed_demo_pool(role_id=ROLE)
    assert counts == {"READY": 5, "REVIEW": 1}
    by_status: dict[str, int] = {}
    for task in protocol.list_tasks():
        by_status[task["status"]] = by_status.get(task["status"], 0) + 1
    assert by_status["READY"] == 5 and by_status["REVIEW"] == 1
    review_task = next(t for t in protocol.list_tasks() if t["status"] == "REVIEW")
    assert review_task["implementer_executor_id"] == "doubao"
    assert all(s["running"] == 0 for s in orchestrator.list_sessions())
    # Re-seeding resets rather than accumulates.
    orchestrator.seed_demo_pool(role_id=ROLE)
    assert len(protocol.list_tasks()) == 6


def test_console_state_carries_capabilities_and_sessions(db_path) -> None:
    orchestrator.ensure_orchestrator()
    state = orchestrator.console_state()
    assert [n["id"] for n in state["nodes"]] == list(orchestrator.NODES)
    by_id = {e["executor_id"]: e for e in state["executors"]}
    assert "implement" in by_id["doubao"]["capabilities"]
    assert by_id["doubao"]["capabilities"]["acceptance"]["locked"] == 1
    assert "implement" in by_id["doubao"]["sessions"]
