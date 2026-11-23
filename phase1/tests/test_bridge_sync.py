#!/usr/bin/env python3
"""Tests for event-driven bridge -> console projection."""
from __future__ import annotations

import json

import bridge_sync
import protocol


def _console_task(db_path, tid="T-900", status="READY"):
    protocol.ensure_schema()
    with protocol.get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO role_specs (role_id,name,content_hash,content,created_at) "
            "VALUES ('r1','r1','h','c',?)", (protocol.utcnow(),))
        conn.commit()
    protocol.create_task(tid, "t", "d", "r1", "ac")
    if status == "BACKLOG":
        return tid
    protocol.ready_task(tid)
    return tid


def _bridge(tmp_path, *, cards=(), events=()):
    root = tmp_path / "bridge"
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "notifications").mkdir(parents=True, exist_ok=True)
    for card in cards:
        (root / "tasks" / f"{card['task_id']}.json").write_text(
            json.dumps(card, ensure_ascii=False), encoding="utf-8")
    if events:
        (root / "notifications" / "outbox.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
            encoding="utf-8")
    return root


def test_bootstrap_aligns_existing_claimed_card(db_path, tmp_path):
    tid = _console_task(db_path, status="BACKLOG")
    root = _bridge(tmp_path, cards=[{
        "task_id": tid,
        "execution": {"status": "claimed", "owner": "doubao-woaijiangjiang", "attempt": 1},
    }])
    report = bridge_sync.sync_once(root)
    assert report["bootstrapped"] is True
    assert protocol.get_task(tid)["status"] == "CLAIMED"
    assert protocol.get_task(tid)["claimed_by"] == "doubao-woaijiangjiang"


def test_incremental_event_moves_delivered_task_to_review(db_path, tmp_path):
    tid = _console_task(db_path)
    root = _bridge(tmp_path, events=[
        {"seq": 1, "task_id": tid, "owner": "doubao-shenlanking",
         "card_status": "claimed", "kind": "claimed"},
        {"seq": 2, "task_id": tid, "owner": "doubao-shenlanking",
         "card_status": "awaiting_review", "kind": "delivered"},
    ])
    bridge_sync.sync_once(root)
    assert protocol.get_task(tid)["status"] == "REVIEW"
    assert bridge_sync._get_seq() == 2


def test_sync_is_idempotent_and_resumes_from_last_seq(db_path, tmp_path):
    tid = _console_task(db_path)
    root = _bridge(tmp_path, events=[
        {"seq": 1, "task_id": tid, "owner": "o", "card_status": "claimed", "kind": "claimed"},
    ])
    first = bridge_sync.sync_once(root)
    assert first["applied"] and protocol.get_task(tid)["status"] == "CLAIMED"
    # Replay: no new events, state and seq unchanged, no error.
    second = bridge_sync.sync_once(root)
    assert second["applied"] == [] and second["last_seq"] == 1
    assert protocol.get_task(tid)["status"] == "CLAIMED"
    # A later event is consumed while the old one is skipped.
    outbox = root / "notifications" / "outbox.jsonl"
    with outbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 3, "task_id": tid, "owner": "o",
                             "card_status": "blocked", "kind": "blocked"}) + "\n")
    third = bridge_sync.sync_once(root)
    assert [e["seq"] for e in third["applied"]] == [3]
    assert protocol.get_task(tid)["status"] == "BLOCKED"


def test_unknown_task_event_is_ignored_safely(db_path, tmp_path):
    root = _bridge(tmp_path, events=[
        {"seq": 1, "task_id": "T-GHOST", "owner": "o",
         "card_status": "claimed", "kind": "claimed"},
    ])
    report = bridge_sync.sync_once(root)  # must not raise
    assert report["applied"] and protocol.get_task("T-GHOST") is None


def test_needs_human_event_does_not_force_accepted_console_task(db_path, tmp_path):
    """A console task already parked in NEEDS_HUMAN must stay there.

    The minimax reviewer leads review verdicts and writes the console state
    directly; its bridge needs_human card event is collapsed from two console
    states (ACCEPTED awaiting integration, NEEDS_HUMAN awaiting a human action).
    Replaying it against a NEEDS_HUMAN task must be a safe no-op (the task is
    not in bridge_sync's linear _FORWARD path), not an error that stalls seq and
    not a forced promotion to ACCEPTED.
    """
    tid = _console_task(db_path, tid="T-nh")
    protocol.atomic_claim(tid, agent_id="doubao-owner", executor_id="doubao")
    for nxt in ("IN_PROGRESS", "SUBMITTED", "REVIEW", "NEEDS_HUMAN"):
        protocol.transition(tid, nxt, "minimax-reviewer" if nxt == "NEEDS_HUMAN" else "doubao-owner",
                            f"drive -> {nxt}")
    assert protocol.get_task(tid)["status"] == "NEEDS_HUMAN"
    root = _bridge(tmp_path, events=[
        {"seq": 1, "task_id": tid, "owner": "doubao-owner",
         "card_status": "needs_human", "kind": "needs_human"},
    ])
    report = bridge_sync.sync_once(root)  # must not raise or stall the seq
    assert report["ok"] is True and report["last_seq"] == 1
    assert protocol.get_task(tid)["status"] == "NEEDS_HUMAN"


def test_reassigned_event_returns_the_claim_to_the_pool(db_path, tmp_path):
    """A reassignment must free the claim the old owner is holding.

    The console has no CLAIMED->READY edge and no API that rewrites
    claimed_by, so without a transfer projection the task stays pinned to the
    old owner forever: every later 'claimed' event is a no-op because
    _ensure_claimed() returns early on an already-claimed task.
    """
    tid = _console_task(db_path, tid="T-xfer")
    protocol.atomic_claim(tid, agent_id="old-owner", executor_id="doubao")
    assert protocol.get_task(tid)["claimed_by"] == "old-owner"

    root = _bridge(tmp_path, events=[
        {"seq": 1, "task_id": tid, "owner": "new-owner",
         "card_status": "dispatched", "kind": "reassigned",
         "detail": {"from_owner": "old-owner", "to_owner": "new-owner"}},
    ])
    report = bridge_sync.sync_once(root)
    assert report["ok"] is True and report["last_seq"] == 1

    task = protocol.get_task(tid)
    assert task["status"] == "READY"
    assert task["claimed_by"] is None

    # The transfer is recorded in the audit trail, not applied silently.
    with protocol.get_db() as conn:
        rows = conn.execute(
            "SELECT from_status, to_status, evidence FROM audit_log WHERE task_id=?",
            (tid,)).fetchall()
    assert any(r["to_status"] == "READY" and "old-owner" in (r["evidence"] or "")
               for r in rows)


def test_new_owner_claims_the_task_after_the_transfer(db_path, tmp_path):
    """After the transfer the new owner's own 'claimed' event must land it."""
    tid = _console_task(db_path, tid="T-xfer2")
    protocol.atomic_claim(tid, agent_id="old-owner", executor_id="doubao")
    root = _bridge(tmp_path, events=[
        {"seq": 1, "task_id": tid, "owner": "new-owner",
         "card_status": "dispatched", "kind": "reassigned",
         "detail": {"from_owner": "old-owner", "to_owner": "new-owner"}},
        {"seq": 2, "task_id": tid, "owner": "new-owner",
         "card_status": "claimed", "kind": "claimed"},
    ])
    report = bridge_sync.sync_once(root)
    assert report["ok"] is True and report["last_seq"] == 2
    task = protocol.get_task(tid)
    assert task["status"] == "CLAIMED"
    assert task["claimed_by"] == "new-owner"


def test_reassigned_event_for_unknown_task_is_ignored(db_path, tmp_path):
    root = _bridge(tmp_path, events=[
        {"seq": 1, "task_id": "T-GHOST", "owner": "new-owner",
         "card_status": "dispatched", "kind": "reassigned",
         "detail": {"from_owner": "old-owner", "to_owner": "new-owner"}},
    ])
    report = bridge_sync.sync_once(root)  # must not raise
    assert report["ok"] is True and report["last_seq"] == 1
    assert protocol.get_task("T-GHOST") is None


def test_reassigned_event_without_target_owner_is_reported(db_path, tmp_path):
    """A malformed transfer must not be applied and must not stall the seq silently."""
    tid = _console_task(db_path, tid="T-xfer3")
    protocol.atomic_claim(tid, agent_id="old-owner", executor_id="doubao")
    root = _bridge(tmp_path, events=[
        {"seq": 1, "task_id": tid, "owner": "",
         "card_status": "dispatched", "kind": "reassigned",
         "detail": {"from_owner": "old-owner"}},
    ])
    report = bridge_sync.sync_once(root)
    assert report["ok"] is False
    assert "reassigned" in report["error"]
    # The claim is untouched and the event is retried on the next poll.
    assert protocol.get_task(tid)["claimed_by"] == "old-owner"
    assert bridge_sync._get_seq() == 0


def test_rework_requested_event_moves_review_task_back_to_rework(db_path, tmp_path):
    """A rework hand-back is a backward move the forward path cannot express."""
    tid = _console_task(db_path, tid="T-rw")
    protocol.atomic_claim(tid, agent_id="owner", executor_id="doubao")
    for nxt in ("IN_PROGRESS", "SUBMITTED", "REVIEW"):
        protocol.transition(tid, nxt, "owner", f"drive -> {nxt}")
    assert protocol.get_task(tid)["status"] == "REVIEW"

    root = _bridge(tmp_path, events=[
        {"seq": 1, "task_id": tid, "owner": "owner",
         "card_status": "retry_requested", "kind": "rework_requested"},
    ])
    report = bridge_sync.sync_once(root)
    assert report["ok"] is True and report["last_seq"] == 1
    assert protocol.get_task(tid)["status"] == "REWORK"


def test_rework_requested_event_on_a_non_review_task_is_a_noop(db_path, tmp_path):
    """REWORK is not in the forward path; only a REVIEW task can be pushed back."""
    tid = _console_task(db_path, tid="T-rw2")
    protocol.atomic_claim(tid, agent_id="owner", executor_id="doubao")
    root = _bridge(tmp_path, events=[
        {"seq": 1, "task_id": tid, "owner": "owner",
         "card_status": "retry_requested", "kind": "rework_requested"},
    ])
    report = bridge_sync.sync_once(root)
    assert report["ok"] is True and report["last_seq"] == 1
    assert protocol.get_task(tid)["status"] == "CLAIMED"

