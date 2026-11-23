#!/usr/bin/env python3
r"""Hermetic tests for the read-only mailbox probe.

The probe must reproduce the doubao-task-worker SKILL step-1 scan rule exactly
and classify the joined worktree+card state into WORK_READY / IN_REVIEW /
DRAINED, including the "delivered but DONE marker missing" false positive.
Everything runs against tmp_path trees; no ssh, git, or real mailbox.
"""
from __future__ import annotations

import json

import mailbox_probe as mp


def _worktree(wt_root, tid: str, *, dispatch=True, done=False, blocked=False,
              owner=None):
    d = wt_root / f"{tid}-doubao"
    (d / "logs").mkdir(parents=True)
    if dispatch:
        doc = {"task_id": tid, "owner": owner or f"owner-of-{tid.lower()}"}
        (d / f"DISPATCH-{tid}.json").write_text(json.dumps(doc), encoding="utf-8")
    if done:
        (d / "logs" / f"DONE-{tid}.txt").write_text("sha\n", encoding="utf-8")
    if blocked:
        (d / "logs" / f"BLOCKED-{tid}.txt").write_text("reason\n", encoding="utf-8")
    return d


def _card(bridge_root, tid: str, owner: str, status: str):
    tasks = bridge_root / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / f"{tid}.json").write_text(json.dumps({
        "task_id": tid,
        "execution": {"owner": owner, "status": status},
    }), encoding="utf-8")


def test_work_ready_mixes_genuine_and_suspect(tmp_path):
    wt, bridge = tmp_path / "wt", tmp_path / "bridge"
    # T-1: brand-new dispatch, no card yet -> genuinely ready.
    _worktree(wt, "T-1", owner="doubao-shenlanking")
    # T-2/T-3: terminal markers must be excluded even with a dispatch present.
    _worktree(wt, "T-2", done=True)
    _worktree(wt, "T-3", blocked=True)
    # T-4: no dispatch paper -> invisible to the scan.
    _worktree(wt, "T-4", dispatch=False)
    # T-5: scan-positive but already delivered -> marker drift, suspect only.
    _worktree(wt, "T-5", owner="doubao-woaijiangjiang")
    _card(bridge, "T-5", "doubao-woaijiangjiang", "awaiting_review")
    # A non-terminal card whose worktree is not scan-ready still counts in flight.
    _card(bridge, "T-6", "doubao-shenlanking", "in_progress")
    _card(bridge, "T-7", "doubao-shenlanking", "needs_human")

    result = mp.probe(wt, bridge)

    assert result["verdict"] == "WORK_READY"
    assert result["ready"] == ["T-1", "T-5"]
    assert result["genuine_ready"] == ["T-1"]
    assert result["suspect_missing_marker"] == ["T-5"]
    assert result["worktree_total"] == 5
    assert {c["tid"] for c in result["in_flight"]} == {"T-5", "T-6"}
    assert [c["tid"] for c in result["review_pending"]] == ["T-5"]
    assert result["terminal_cards"] == 1
    summary = result["owner_summary"]
    assert summary["doubao-shenlanking"]["in_flight"] == 1
    assert summary["doubao-woaijiangjiang"]["review_pending"] == 1
    assert result["errors"] == []


def test_in_review_when_no_new_mail_but_cards_open(tmp_path):
    wt, bridge = tmp_path / "wt", tmp_path / "bridge"
    _worktree(wt, "T-2", done=True)
    _card(bridge, "T-2", "doubao-shenlanking", "auditing")

    result = mp.probe(wt, bridge)

    assert result["verdict"] == "IN_REVIEW"
    assert result["ready"] == []
    assert result["review_pending"][0]["tid"] == "T-2"


def test_drained_when_everything_terminal(tmp_path):
    wt, bridge = tmp_path / "wt", tmp_path / "bridge"
    _worktree(wt, "T-2", done=True)
    _card(bridge, "T-2", "doubao-shenlanking", "needs_human")
    _card(bridge, "T-9", "doubao-woaijiangjiang", "blocked")

    result = mp.probe(wt, bridge)

    assert result["verdict"] == "DRAINED"
    assert result["in_flight"] == []
    assert result["terminal_cards"] == 2


def test_corrupt_files_are_reported_not_raised(tmp_path):
    wt, bridge = tmp_path / "wt", tmp_path / "bridge"
    bad = _worktree(wt, "T-1")
    (bad / "DISPATCH-T-1.json").write_text("{ not json", encoding="utf-8")
    tasks = bridge / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "T-9.json").write_text("@@@", encoding="utf-8")

    result = mp.probe(wt, bridge)

    assert result["verdict"] == "WORK_READY"  # dispatch file still exists
    assert len(result["errors"]) == 2
    assert result["ready_detail"][0]["owner"] is None


def test_missing_roots_drain_without_exception(tmp_path):
    result = mp.probe(tmp_path / "nope-wt", tmp_path / "nope-bridge")
    assert result["verdict"] == "DRAINED"
    assert result["worktree_total"] == 0
    assert result["errors"]  # the missing worktree root is explained


def test_pending_only_covers_cards_the_owner_can_still_claim(tmp_path):
    wt, bridge = tmp_path / "wt", tmp_path / "bridge"
    # Brand new dispatch, card still "dispatched" -> the owner has to claim it.
    _worktree(wt, "T-1", owner="doubao-woaijiangjiang")
    _card(bridge, "T-1", "doubao-woaijiangjiang", "dispatched")
    # Already claimed by its owner -> the scan still fires but nobody is waiting.
    _worktree(wt, "T-2", owner="doubao-woaijiangjiang")
    _card(bridge, "T-2", "doubao-woaijiangjiang", "claimed")
    # Rejected delivery -> claimable again, that is exactly the rework case.
    _worktree(wt, "T-3", owner="doubao-shenlanking")
    _card(bridge, "T-3", "doubao-shenlanking", "retry_requested")
    # Delivered -> marker drift only.
    _worktree(wt, "T-4", owner="doubao-shenlanking")
    _card(bridge, "T-4", "doubao-shenlanking", "awaiting_review")
    # Scan-ready but no card at all -> nobody can legally claim it.
    _worktree(wt, "T-5", owner="doubao-shenlanking")

    result = mp.probe(wt, bridge)

    assert [item["tid"] for item in result["pending"]] == ["T-1", "T-3"]
    assert result["pending_owners"] == {
        "doubao-woaijiangjiang": ["T-1"],
        "doubao-shenlanking": ["T-3"],
    }
    assert result["ready_no_card"] == ["T-5"]
    assert result["pending"][0]["status"] == "dispatched"
    assert result["pending"][1]["status"] == "retry_requested"


def test_pending_carries_the_rework_facts(tmp_path):
    wt, bridge = tmp_path / "wt", tmp_path / "bridge"
    worktree = _worktree(wt, "T-7", owner="doubao-shenlanking")
    (worktree / "logs" / "REWORK-T-7.txt").write_text("redo\n", encoding="utf-8")
    (worktree / "DISPATCH-T-7.json").write_text(json.dumps({
        "task_id": "T-7",
        "owner": "doubao-shenlanking",
        "rework": {"reason": "auditor said no", "next_attempt": 2},
    }), encoding="utf-8")
    _card(bridge, "T-7", "doubao-shenlanking", "retry_requested")

    result = mp.probe(wt, bridge)

    assert len(result["pending"]) == 1
    entry = result["pending"][0]
    assert entry["rework"] is True
    assert entry["rework_reason"] == "auditor said no"
    assert entry["next_attempt"] == 2
    # The scan row itself also carries the rework note.
    row = result["ready_detail"][0]
    assert row["rework_note"] is True
