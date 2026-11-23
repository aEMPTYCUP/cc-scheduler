#!/usr/bin/env python3
"""Tests for the real Doubao mailbox adapter (fake bridge hooks, no ssh/tokens)."""
from __future__ import annotations

import pytest

import orchestrator
import protocol
from real_adapters import DoubaoAdapter, reconcile


class FakeBridge:
    """In-memory stand-in for the agent-bridge file mailbox."""

    def __init__(self):
        self.cards = {}
        self.dispatches = []

    def reader(self, task_id):
        return self.cards.get(task_id)

    def hook(self, task, adapter):
        self.dispatches.append(task["task_id"])
        assert adapter.owner == "doubao-shenlanking"
        self.cards[task["task_id"]] = {"execution": {"status": "dispatched"}}
        return self.cards[task["task_id"]]

    def set_card(self, task_id, status):
        self.cards[task_id] = {"execution": {"status": status}}


def _ready_task(tid="T-500"):
    protocol.ensure_schema()
    with protocol.get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO role_specs (role_id, name, content_hash, content, created_at) "
            "VALUES ('r1','r1','h','c',?)", (protocol.utcnow(),))
        conn.commit()
    protocol.create_task(tid, "标题", "描述", "r1", "验收", priority="high")
    protocol.ready_task(tid)
    return tid


def _adapter(fake: FakeBridge) -> DoubaoAdapter:
    return DoubaoAdapter(
        bridge_root="C:/fake/bridge", owner="doubao-shenlanking",
        worktree_root="C:/fake/wt", dispatch_hook=fake.hook, card_reader=fake.reader,
    )


def test_adapter_is_real_not_simulated():
    assert DoubaoAdapter.__mro__[1].__name__ == "WorkerAdapter"
    assert _adapter(FakeBridge()).is_simulated is False


def test_implement_dispatches_once_and_parks_in_progress(db_path):
    fake = FakeBridge()
    adapter = _adapter(fake)
    tid = _ready_task()
    summary = adapter.implement(protocol.get_task(tid), "doubao")
    assert "dispatched" in summary
    assert fake.dispatches == [tid]
    task = protocol.get_task(tid)
    assert task["status"] == "IN_PROGRESS"               # never auto-jumps to REVIEW
    assert task["implementer_executor_id"] == "doubao"   # channel is frozen
    assert task["claimed_by"] == "doubao"


def test_implement_is_idempotent_no_second_dispatch(db_path):
    fake = FakeBridge()
    adapter = _adapter(fake)
    tid = _ready_task()
    adapter.implement(protocol.get_task(tid), "doubao")
    # Second tick: card already exists, console already in progress.
    summary = adapter.implement(protocol.get_task(tid), "doubao")
    assert fake.dispatches == [tid]
    assert "already in flight" in summary
    assert protocol.get_task(tid)["status"] == "IN_PROGRESS"


def test_accept_and_integrate_are_refused(db_path):
    adapter = _adapter(FakeBridge())
    tid = _ready_task()
    with pytest.raises(PermissionError):
        adapter.accept(protocol.get_task(tid), "doubao")
    with pytest.raises(PermissionError):
        adapter.integrate(protocol.get_task(tid), "doubao")


def test_reconcile_walks_card_statuses_along_legal_edges(db_path):
    fake = FakeBridge()
    adapter = _adapter(fake)
    tid = _ready_task()
    adapter.implement(protocol.get_task(tid), "doubao")
    assert protocol.get_task(tid)["status"] == "IN_PROGRESS"

    fake.set_card(tid, "awaiting_review")
    assert reconcile(adapter)[0]["action"] == "SUBMITTED"
    assert protocol.get_task(tid)["status"] == "SUBMITTED"

    fake.set_card(tid, "auditing")
    assert [c for c in reconcile(adapter) if c["task_id"] == tid][0]["action"] == "REVIEW"
    assert protocol.get_task(tid)["status"] == "REVIEW"

    fake.set_card(tid, "needs_human")  # audit PASS -> accepted, awaits human merge
    assert [c for c in reconcile(adapter) if c["task_id"] == tid][0]["action"] == "ACCEPTED"
    assert protocol.get_task(tid)["status"] == "ACCEPTED"

    # Idempotent: an unchanged card past the edge yields no new transition.
    again = [c for c in reconcile(adapter) if c["task_id"] == tid]
    assert again == []


def test_reconcile_blocked_moves_console_to_blocked(db_path):
    fake = FakeBridge()
    adapter = _adapter(fake)
    tid = _ready_task()
    adapter.implement(protocol.get_task(tid), "doubao")
    fake.set_card(tid, "blocked")
    out = reconcile(adapter)
    assert out[0]["action"] == "BLOCK"
    assert protocol.get_task(tid)["status"] == "BLOCKED"


def test_reconcile_skips_illegal_edge_without_raising(db_path):
    # Console still IN_PROGRESS; a REWORK target is only legal from REVIEW.
    fake = FakeBridge()
    adapter = _adapter(fake)
    tid = _ready_task()
    adapter.implement(protocol.get_task(tid), "doubao")
    fake.set_card(tid, "retry_requested")
    out = [c for c in reconcile(adapter) if c["task_id"] == tid]
    assert out and out[0]["action"] == "skipped"
    assert protocol.get_task(tid)["status"] == "IN_PROGRESS"  # untouched, no crash


def test_production_pool_runs_real_adapter_through_worker(db_path, monkeypatch):
    orchestrator.ensure_orchestrator()
    protocol.set_pool_mode("production")
    # A real adapter registers itself as non-simulated, lifting the guard.
    monkeypatch.setattr(orchestrator, "_active_adapter_simulated", False)
    fake = FakeBridge()
    adapter = _adapter(fake)
    tid = _ready_task()

    result = orchestrator.start_worker("doubao", "implement")  # not 403-guarded
    assert result["running"] is True
    try:
        once = orchestrator.run_worker_once("doubao", "implement", adapter)
        assert once["status"] == "worked"
        assert fake.dispatches == [tid]
        assert protocol.get_task(tid)["status"] == "IN_PROGRESS"
    finally:
        orchestrator.stop_worker("doubao", "implement")
