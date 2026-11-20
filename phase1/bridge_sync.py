#!/usr/bin/env python3
"""Project agent-bridge card events onto the console task table (read model).

Event-driven replacement for whole-pool polling. The bridge appends one JSON
line per responsibility-card transition to ``<bridge_root>/notifications/outbox.jsonl``;
this module consumes new events (tracking the last applied sequence in
pool_meta) and advances the console task along legal state-machine edges.

On first run it bootstraps from the current responsibility cards so a task that
moved before the outbox existed is still reflected. Every projection is
idempotent and tolerant of an illegal edge (it skips rather than raises), so a
replayed event never corrupts the pool. Pure file reads on the bridge side keep
cc-scheduler decoupled from the agent_bridge package.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import protocol

DEFAULT_BRIDGE_ROOT = Path(os.environ.get("DSH_BRIDGE_ROOT", r"C:/Users/32831/wt/bridge-live"))
SEQ_KEY = "bridge_outbox_seq"
DOUBAO_EXECUTOR = "doubao"

# Bridge card status -> target console status (fallback when no explicit kind).
_TARGET_STATUS = {
    "claimed": "CLAIMED",
    "dispatched": "READY",
    "awaiting_review": "REVIEW",
    "auditing": "REVIEW",
    "needs_human": "ACCEPTED",
    "retry_requested": "REWORK",
    "blocked": "BLOCKED",
}
# The outbox event kind is the authoritative signal (a block event keeps the
# card on "claimed" until the worker routes it, so card_status alone is wrong).
_KIND_TARGET = {
    "claimed": "CLAIMED",
    "dispatched": "READY",
    "delivered": "REVIEW",
    "blocked": "BLOCKED",
    "needs_human": "ACCEPTED",
    "retry_requested": "REWORK",
    # agent_bridge/rework.py emits this kind when it hands a rejected
    # delivery back to the owner; the card status is 'retry_requested'.
    "rework_requested": "REWORK",
}
# Outbox kinds that mean "the task now belongs to a different agent". They are
# not a status move: the card is 'dispatched' again (the new owner still has to
# claim it), so the console has to give the old owner its claim back.
_OWNER_TRANSFER_KINDS = {"reassigned"}
# Linear forward path a bridge-driven task follows in the console.
_FORWARD = ["BACKLOG", "READY", "CLAIMED", "IN_PROGRESS", "SUBMITTED",
            "REVIEW", "ACCEPTED", "INTEGRATION", "DONE"]
# States from which a fresh claim/delivery event must reopen the task.
_REOPEN_STATES = {"REWORK", "BLOCKED", "FAILED", "NEEDS_HUMAN"}


def _read_outbox(bridge_root: Path) -> List[Dict[str, Any]]:
    path = bridge_root / "notifications" / "outbox.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _get_seq() -> int:
    with protocol.get_db() as conn:
        row = conn.execute("SELECT value FROM pool_meta WHERE key=?", (SEQ_KEY,)).fetchone()
    return int(row["value"]) if row else 0


def _set_seq(seq: int) -> None:
    with protocol.get_db() as conn:
        conn.execute(
            "INSERT INTO pool_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SEQ_KEY, str(seq)),
        )
        conn.commit()


def _walk_forward(task_id: str, target: str, actor: str) -> None:
    """Transition a task that is already CLAIMED along legal edges to target.

    BACKLOG->READY and READY->CLAIMED are NOT transition() edges (they belong
    to ready_task/atomic_claim); callers must reach CLAIMED via
    _ensure_claimed first. Raises ValueError if an edge is illegal so the
    caller can stop without advancing the consumed sequence.
    """
    current = protocol.get_task(task_id)
    if current is None or target not in _FORWARD:
        return
    try:
        ci, ti = _FORWARD.index(current["status"]), _FORWARD.index(target)
    except ValueError:
        return
    if ci >= ti:
        return  # already at or past the target
    for nxt in _FORWARD[ci + 1:ti + 1]:
        protocol.transition(task_id, nxt, actor,
                            f"bridge event -> {nxt}",
                            actor_executor_id=DOUBAO_EXECUTOR)


def _reopen_to_ready(task_id: str) -> None:
    """Reset a bounced task (REWORK/BLOCKED/FAILED/NEEDS_HUMAN) back to READY.

    A fresh delivery means the worker produced new work, so the task must
    re-enter the claim -> review path regardless of its previous terminal-ish
    state. Direct status reset (no transition edge exists for these backwards
    moves), preserving the audit trail.
    """
    with protocol.get_db() as conn:
        conn.execute(
            "UPDATE tasks SET status='READY', claimed_by=NULL, implementer_executor_id=NULL, "
            "claimed_at=NULL, updated_at=? WHERE task_id=?",
            (protocol.utcnow(), task_id))
        conn.commit()


def _ensure_claimed(task_id: str, owner: str) -> None:
    """Bring a BACKLOG/READY task up to CLAIMED on behalf of the bridge owner."""
    task = protocol.get_task(task_id)
    if task is None or task["status"] in ("CLAIMED", "IN_PROGRESS", "SUBMITTED",
                                           "REVIEW", "ACCEPTED", "INTEGRATION", "DONE"):
        return
    if task["status"] == "BACKLOG":
        protocol.ready_task(task_id, actor="bridge-sync")
    if protocol.get_task(task_id)["status"] == "READY":
        protocol.atomic_claim(task_id, agent_id=owner, executor_id=DOUBAO_EXECUTOR)


def _project(task_id: str, target: str, owner: str) -> None:
    task = protocol.get_task(task_id)
    if task is None:
        return  # console pool does not know this task yet; ignore
    if target == "BLOCKED":
        if task["status"] not in ("BLOCKED", "DONE"):
            protocol.block_task(task_id, owner, "bridge blocked_reported")
        return
    if target == "REWORK":
        # A rework request is a backward move (REVIEW -> REWORK) that the
        # forward path cannot express. The actor is deliberately NOT the
        # doubao executor: protocol.transition() refuses a review decision
        # whose reviewer equals the implementer, and the projector is not the
        # implementer.
        if task["status"] == "REVIEW":
            protocol.transition(task_id, "REWORK", "bridge-sync",
                                "bridge event -> REWORK")
        return
    # A fresh claim/delivery reopens a task that was bounced for rework.
    if task["status"] in _REOPEN_STATES and target in ("CLAIMED", "READY", "REVIEW"):
        _reopen_to_ready(task_id)
    # Every forward target first requires the task to be claimed.
    _ensure_claimed(task_id, owner)
    if target != "CLAIMED":
        _walk_forward(task_id, target, owner)


def _project_transfer(task_id: str, new_owner: str, detail: Dict[str, Any]) -> None:
    """Project an owner reassignment onto the console read model.

    The console has no CLAIMED->READY edge and no API that changes
    ``claimed_by``, so without this the old owner keeps the claim forever:
    every later 'claimed' event is a no-op (_ensure_claimed returns early on a
    CLAIMED task) and the task is pinned to whoever held it first. Giving the
    claim back (status READY, unclaimed) matches the card state 'dispatched'
    and lets the new owner's own 'claimed' event land it on the right owner.
    """
    old_owner = detail.get("from_owner")
    task = protocol.get_task(task_id)
    if task is None:
        return
    if task["status"] == "BACKLOG":
        # A task the console has never released still needs the legal
        # BACKLOG->READY edge before it can sit in the unclaimed pool.
        protocol.ready_task(task_id, actor="bridge-sync")
    reason = "bridge reassigned: %s -> %s" % (old_owner or "?", new_owner)
    # The claim goes back to the pool (status READY, claimed_by NULL). The card
    # is 'dispatched' again, so the new owner still has to claim it; when that
    # 'claimed' event arrives, _ensure_claimed puts it on the new owner.
    protocol.reassign_claim(task_id, new_owner, actor="bridge-sync", reason=reason)


def _bootstrap(bridge_root: Path) -> None:
    """Align with responsibility cards that pre-date the outbox."""
    tasks_dir = bridge_root / "tasks"
    if not tasks_dir.is_dir():
        return
    for card_path in sorted(tasks_dir.glob("*.json")):
        card = json.loads(card_path.read_text(encoding="utf-8"))
        execution = card.get("execution", {})
        target = _TARGET_STATUS.get(execution.get("status"))
        if target:
            try:  # one bad card must not block the others
                _project(card["task_id"], target, execution.get("owner", "bridge"))
            except Exception:  # noqa: BLE001 - projection is best-effort at bootstrap
                continue


def sync_once(bridge_root: Optional[Path] = None) -> Dict[str, Any]:
    """Consume new bridge events; returns a summary. Safe to call on every poll."""
    root = Path(bridge_root or DEFAULT_BRIDGE_ROOT)
    protocol.ensure_schema()
    bootstrapped = False
    if _get_seq() == 0:
        _bootstrap(root)
        bootstrapped = True

    last_seq = _get_seq()
    applied = []
    max_seq = last_seq
    for event in _read_outbox(root):
        seq = int(event.get("seq", 0))
        if seq <= last_seq:
            continue
        kind = event.get("kind", "")
        try:
            if kind in _OWNER_TRANSFER_KINDS:
                detail = event.get("detail") or {}
                new_owner = (detail.get("to_owner") or event.get("owner") or "").strip()
                if not new_owner:
                    raise ValueError("reassigned event without a target owner")
                _project_transfer(event["task_id"], new_owner, detail)
            else:
                # Kind is authoritative (a block event keeps card_status=claimed);
                # fall back to the card status for older/kind-less events.
                target = _KIND_TARGET.get(kind) or _TARGET_STATUS.get(
                    event.get("card_status")) or "REVIEW"
                _project(event["task_id"], target, event.get("owner", "bridge"))
        except Exception as exc:  # noqa: BLE001 - stop and keep seq so it retries next poll
            return {"ok": False, "bootstrapped": bootstrapped, "last_seq": max_seq,
                    "applied": applied, "error": f"seq {seq}: {type(exc).__name__}: {exc}"}
        applied.append({"seq": seq, "task_id": event["task_id"],
                        "card_status": event.get("card_status"), "kind": kind})
        max_seq = seq
    if max_seq != last_seq:
        _set_seq(max_seq)
    return {"ok": True, "bootstrapped": bootstrapped,
            "last_seq": max_seq, "applied": applied}
