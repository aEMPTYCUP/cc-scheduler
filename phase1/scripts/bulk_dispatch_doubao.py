#!/usr/bin/env python3
"""Bulk-dispatch all currently-runnable tasks across the two Doubao accounts.

A dedicated "express lane": unlike the buffer=1 feeder, this claims every task
whose dependencies are already DONE (regardless of NEEDS_HUMAN/BLOCKED history,
which is reactivated to READY first) and creates one bridge responsibility card
per task, round-robin across the registered Doubao owners. Each account ends up
with an ordered list of cards; the Mac Doubao skill loops through its own cards.

Idempotent: a task that already has a non-terminal bridge card is skipped.
Creates an isolated git worktree per task. Never pushes and never touches the
formal queue.json (only the console SQLite pool + bridge mailbox).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PHASE1 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PHASE1))
sys.path.insert(0, str(PHASE1 / "scripts"))
import protocol  # noqa: E402

BRIDGE_PKG = "C:/Users/32831/wt/agent-bridge-implementation/.claude"
if BRIDGE_PKG not in sys.path:
    sys.path.insert(0, BRIDGE_PKG)
from agent_bridge.dispatcher import (  # noqa: E402
    build_dispatch_document, dispatch_task, write_dispatch_file,
)
from agent_bridge.store import BridgeStore  # noqa: E402

REPO = r"C:/Users/32831/OneDrive/Desktop/期货分析"
BRIDGE_ROOT = Path(r"C:/Users/32831/wt/bridge-live")
WORKTREE_ROOT = Path(r"C:/Users/32831/wt")
DOUBAO_OWNERS = ["doubao-shenlanking", "doubao-woaijiangjiang"]
# Tasks already past dispatch (do not re-queue).
SKIP_STATUS = {"REVIEW", "ACCEPTED", "INTEGRATION", "DONE", "IN_PROGRESS", "SUBMITTED"}
TERMINAL_CARD = {"needs_human", "blocked", "done", "passed"}


def _existing_card(store: BridgeStore, tid: str):
    try:
        card = store.load_task(tid)
    except FileNotFoundError:
        return None
    if card.get("execution", {}).get("status") in TERMINAL_CARD:
        return None
    return card


def _ensure_worktree(tid: str) -> Path:
    wt = WORKTREE_ROOT / f"{tid}-doubao"
    branch = f"doubao/{tid.lower()}-auto"
    if wt.exists():
        if (wt / ".git").exists():
            return wt  # a real linked worktree
        # Legacy plain directory collides with the expected path: quarantine it
        # (never delete) and create a proper git worktree at the canonical path.
        bak = wt.with_name(f"{wt.name}.legacy-{int(time.time())}")
        wt.rename(bak)
        print(f"  quarantine legacy non-git dir -> {bak.name}")
    made = subprocess.run(
        ["git", "-C", REPO, "worktree", "add", "-b", branch, str(wt), "master"],
        capture_output=True, text=True,
    )
    if made.returncode != 0:
        raise RuntimeError(f"worktree add {tid}: {made.stderr.strip()[:200]}")
    return wt


def main() -> int:
    protocol.ensure_schema()
    store = BridgeStore(BRIDGE_ROOT)
    with protocol.get_db() as conn:
        tasks = [dict(r) for r in conn.execute(
            "SELECT * FROM tasks WHERE status <> 'DONE' ORDER BY "
            "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at")]
        status = {r[0]: r[1] for r in conn.execute("SELECT task_id,status FROM tasks")}

    runnable, blocked = [], []
    for t in tasks:
        tid = t["task_id"]
        if t["status"] in SKIP_STATUS:
            continue
        deps = json.loads(t.get("dependencies_json") or "[]")
        unmet = [d for d in deps if status.get(d) != "DONE"]
        (blocked if unmet else runnable).append((tid, unmet))

    results, owner_cursor = [], {o: 0 for o in DOUBAO_OWNERS}
    for i, (tid, _unmet) in enumerate(runnable):
        owner = DOUBAO_OWNERS[i % len(DOUBAO_OWNERS)]
        try:
            if _existing_card(store, tid):
                results.append({"task_id": tid, "owner": owner, "action": "already-dispatched"})
                continue
            task = protocol.get_task(tid)
            # Reactivate historical NEEDS_HUMAN/BLOCKED/FAILED back to a claimable READY.
            if task["status"] != "READY":
                with protocol.get_db() as conn:
                    conn.execute(
                        "UPDATE tasks SET status='READY', claimed_by=NULL, "
                        "implementer_executor_id=NULL, claimed_at=NULL, last_error=NULL, updated_at=? "
                        "WHERE task_id=? AND status <> 'READY'",
                        (protocol.utcnow(), tid))
                    conn.commit()
            protocol.atomic_claim(tid, agent_id=owner, executor_id="doubao")
            wt = _ensure_worktree(tid)
            spec = {
                "id": tid,
                "title": task["title"],
                "description": task.get("description") or "",
                "allowed_paths": json.loads(task.get("allowed_paths_json") or "[]"),
                "behavioral_check": task.get("behavioral_check") or "",
                "pass_condition": task.get("pass_condition") or "",
            }
            contract = BRIDGE_ROOT / "contracts" / f"contract.{tid}.json"
            contract.parent.mkdir(parents=True, exist_ok=True)
            contract.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            card = dispatch_task(
                store, task_id=tid, owner=owner, worktree=str(wt),
                branch=f"doubao/{tid.lower()}-auto", contract_path=str(contract),
                allowed_paths=spec["allowed_paths"],
                behavioral_check=spec["behavioral_check"],
                pass_condition=spec["pass_condition"], title=spec["title"],
            )
            doc = build_dispatch_document(
                data_root=BRIDGE_ROOT, card=card, allowed_paths=spec["allowed_paths"],
                behavioral_check=spec["behavioral_check"], pass_condition=spec["pass_condition"])
            write_dispatch_file(wt, doc)
            owner_cursor[owner] += 1
            results.append({"task_id": tid, "owner": owner, "action": "dispatched",
                            "card": card["execution"]["status"], "worktree": str(wt)})
        except Exception as exc:  # noqa: BLE001 - keep dispatching the rest
            results.append({"task_id": tid, "owner": owner, "action": "error", "error": f"{type(exc).__name__}: {exc}"})

    print(json.dumps({
        "dispatched_or_kept": results,
        "per_owner_counts": owner_cursor,
        "waiting_on_dependencies": {tid: unmet for tid, unmet in blocked},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
