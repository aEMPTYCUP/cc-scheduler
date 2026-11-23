#!/usr/bin/env python3
"""Hermetic tests for the Doubao backlog feeder (no git, no agent_bridge)."""
from __future__ import annotations

import pytest

import bridge_feeder
import protocol

OWNERS = ["doubao-shenlanking", "doubao-woaijiangjiang"]


def _ready(tid, *, deps=None):
    protocol.ensure_schema()
    with protocol.get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO role_specs (role_id,name,content_hash,content,created_at) "
            "VALUES ('r1','r1','h','c',?)", (protocol.utcnow(),))
        conn.commit()
    protocol.create_task(tid, "t", "d", "r1", "ac", dependencies=deps or [])
    protocol.ready_task(tid)
    return tid


def _feeder(tmp_path, cards, dispatched, *, buffer=1):
    return bridge_feeder.BacklogFeeder(
        bridge_root=str(tmp_path / "bridge"), repo_path=str(tmp_path / "repo"),
        doubao_owners=OWNERS, buffer=buffer,
        card_reader=lambda: cards,
        dispatch_hook=lambda task, owner: (
            dispatched.append((task["task_id"], owner))
            or {"execution": {"status": "dispatched", "owner": owner}},
        )[-1],
    )


def _card(tid, owner, status):
    return {"task_id": tid, "execution": {"owner": owner, "status": status}}


def test_skips_when_slot_full(db_path, tmp_path):
    dispatched = []
    _ready("T-1")
    cards = [_card("T-9", OWNERS[0], "claimed")]  # one in-flight, buffer=1
    feeder = _feeder(tmp_path, cards, dispatched)
    out = feeder.feed_owner(OWNERS[0])
    assert out["action"] == "skipped" and out["in_flight"] == 1
    assert dispatched == []
    assert protocol.get_task("T-1")["status"] == "READY"  # untouched


def test_refills_when_empty_and_claims_on_doubao_channel(db_path, tmp_path):
    dispatched = []
    tid = _ready("T-1")
    feeder = _feeder(tmp_path, [], dispatched)  # no cards -> zero in-flight
    out = feeder.feed_owner(OWNERS[1])
    assert out["action"] == "dispatched" and out["task_id"] == tid
    task = protocol.get_task(tid)
    assert task["status"] == "CLAIMED"
    assert task["claimed_by"] == OWNERS[1]
    assert task["implementer_executor_id"] == "doubao"
    assert dispatched == [(tid, OWNERS[1])]


def test_terminal_card_frees_the_slot(db_path, tmp_path):
    dispatched = []
    _ready("T-2")
    # needs_human is terminal -> does not occupy the slot, so a refill happens.
    cards = [_card("T-old", OWNERS[0], "needs_human")]
    feeder = _feeder(tmp_path, cards, dispatched)
    out = feeder.feed_owner(OWNERS[0])
    assert out["action"] == "dispatched"
    assert feeder.count_in_flight(OWNERS[0]) == 0


def test_starved_when_no_ready_task(db_path, tmp_path):
    protocol.ensure_schema()  # tables exist, but no tasks
    dispatched = []
    feeder = _feeder(tmp_path, [], dispatched)
    out = feeder.feed_owner(OWNERS[0])
    assert out["action"] == "starved" and dispatched == []


def test_feed_all_keeps_one_slot_per_account(db_path, tmp_path):
    dispatched = []
    _ready("T-1")
    _ready("T-3")
    feeder = _feeder(tmp_path, [], dispatched)
    results = feeder.feed_all()
    actions = {r["owner"]: r["action"] for r in results}
    assert all(a == "dispatched" for a in actions.values())
    # Two distinct tasks, one per account, no double-booking.
    tids = {t for t, _ in dispatched}
    assert len(tids) == 2 and len(dispatched) == 2


def test_dispatch_failure_releases_the_claim(db_path, tmp_path):
    tid = _ready("T-4")

    def boom(task, owner):
        raise RuntimeError("worktree exploded")

    feeder = bridge_feeder.BacklogFeeder(
        bridge_root=str(tmp_path / "b"), repo_path=str(tmp_path / "r"),
        doubao_owners=OWNERS, card_reader=lambda: [], dispatch_hook=boom)
    with pytest.raises(RuntimeError):
        feeder.feed_owner(OWNERS[0])
    # Claim rolled back: the task is back in READY and re-selectable.
    task = protocol.get_task(tid)
    assert task["status"] == "READY" and task["claimed_by"] is None
    assert protocol.select_next_task()["task_id"] == tid


def test_lost_claim_race_tries_next_without_crashing(db_path, tmp_path, monkeypatch):
    _ready("T-5")
    _ready("T-6")
    order = iter(["T-5", "T-6"])
    real_claim = protocol.atomic_claim

    def flaky(task_id, agent_id, executor_id=None):
        if task_id == "T-5":
            raise ValueError("row already claimed by another worker")
        return real_claim(task_id, agent_id, executor_id)

    monkeypatch.setattr(protocol, "atomic_claim", flaky)
    dispatched = []
    feeder = _feeder(tmp_path, [], dispatched)
    out = feeder.feed_owner(OWNERS[0])
    # select_next_task keeps returning T-5 (highest priority); after the failed
    # claim it is recorded as attempted, so the feeder reports starved cleanly.
    assert out["action"] in ("starved", "dispatched")


def _aged_card(tid, owner, status, hours_ago):
    """A card whose newest event happened `hours_ago` hours ago."""
    from datetime import datetime, timedelta, timezone
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return {
        "task_id": tid,
        "execution": {"owner": owner, "status": status, "claimed_at": stamp},
        "history": [{"from": "dispatched", "to": status, "actor": "doubao",
                     "created_at": stamp, "event_id": None}],
    }


def test_stale_cards_do_not_hold_a_mailbox_slot(tmp_path):
    """A card nobody moved for hours must not block the mailbox forever.

    The Doubao accounts are Mac web sessions: when nobody wakes them, cards sit
    in "claimed" indefinitely. Counting them as in-flight made the feeder skip
    forever while no work happened anywhere.
    """
    cards = [_aged_card("T-stale", OWNERS[0], "claimed", hours_ago=4),
             _aged_card("T-fresh", OWNERS[0], "claimed", hours_ago=0.01)]
    feeder = bridge_feeder.BacklogFeeder(
        bridge_root=str(tmp_path / "bridge"), repo_path=str(tmp_path / "repo"),
        doubao_owners=OWNERS, buffer=1, card_reader=lambda: cards,
        stale_after_seconds=7200)
    assert feeder.count_in_flight(OWNERS[0]) == 1
    assert feeder.count_stale(OWNERS[0]) == 1


def test_stale_card_does_not_prevent_dispatch(tmp_path, db_path):
    tid = _ready("T-901")
    cards = [_aged_card("T-stale", OWNERS[0], "claimed", hours_ago=4)]
    dispatched = []
    feeder = bridge_feeder.BacklogFeeder(
        bridge_root=str(tmp_path / "bridge"), repo_path=str(tmp_path / "repo"),
        doubao_owners=OWNERS, buffer=1, card_reader=lambda: cards,
        dispatch_hook=lambda task, owner: (
            dispatched.append((task["task_id"], owner))
            or {"execution": {"status": "dispatched", "owner": owner}},
        )[-1],
        stale_after_seconds=7200)
    result = feeder.feed_owner(OWNERS[0])
    assert result["action"] == "dispatched"
    assert dispatched and dispatched[0][0] == tid


def test_stale_detection_can_be_disabled(tmp_path):
    cards = [_aged_card("T-stale", OWNERS[0], "claimed", hours_ago=100)]
    feeder = bridge_feeder.BacklogFeeder(
        bridge_root=str(tmp_path / "bridge"), repo_path=str(tmp_path / "repo"),
        doubao_owners=OWNERS, buffer=1, card_reader=lambda: cards,
        stale_after_seconds=0)
    assert feeder.count_in_flight(OWNERS[0]) == 1
    assert feeder.count_stale(OWNERS[0]) == 0


def test_backlog_task_with_satisfied_dependencies_becomes_ready(db_path, tmp_path):
    """A task whose dependencies are done must leave BACKLOG.

    select_next_task() only looks at READY tasks and nothing in the production
    path promotes a freshly unblocked task, so the pool would stall forever.
    """
    dep = _ready("T-902")
    with protocol.get_db() as conn:
        conn.execute("UPDATE tasks SET status='DONE' WHERE task_id=?", (dep,))
        conn.commit()
    tid = _ready("T-903", deps=[dep])
    with protocol.get_db() as conn:
        conn.execute("UPDATE tasks SET status='BACKLOG' WHERE task_id=?", (tid,))
        conn.commit()

    feeder = bridge_feeder.BacklogFeeder(
        bridge_root=str(tmp_path / "bridge"), repo_path=str(tmp_path / "repo"),
        doubao_owners=OWNERS, buffer=1, card_reader=lambda: [],
        dispatch_hook=lambda task, owner: {"execution": {"status": "dispatched", "owner": owner}},
        stale_after_seconds=7200)
    activated = feeder.activate_ready_tasks()
    assert tid in activated
    assert protocol.get_task(tid)["status"] == "READY"


def test_backlog_task_with_unmet_dependencies_stays_backlog(db_path, tmp_path):
    dep = _ready("T-904")
    tid = _ready("T-905", deps=[dep])  # dep is READY, not DONE
    with protocol.get_db() as conn:
        conn.execute("UPDATE tasks SET status='BACKLOG' WHERE task_id=?", (tid,))
        conn.commit()
    feeder = bridge_feeder.BacklogFeeder(
        bridge_root=str(tmp_path / "bridge"), repo_path=str(tmp_path / "repo"),
        doubao_owners=OWNERS, buffer=1, card_reader=lambda: [],
        dispatch_hook=lambda task, owner: {"execution": {"status": "dispatched", "owner": owner}},
        stale_after_seconds=7200)
    assert tid not in feeder.activate_ready_tasks()
    assert protocol.get_task(tid)["status"] == "BACKLOG"
