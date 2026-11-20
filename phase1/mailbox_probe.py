#!/usr/bin/env python3
"""Read-only Doubao mailbox probe (no token, no CLI, no mailbox writes).

The two Doubao accounts are macOS web sessions with no local CLI: they only
learn about work by scanning the Windows worktree directory for dispatch
papers (see the ``doubao-task-worker`` SKILL, step 1). A web session cannot be
driven by launchd, so the hourly timer on the Mac runs *this* probe over ssh
and only notifies a human who can wake the relevant account.

The probe reproduces the SKILL's exact scan rule:

    a worktree ``<wt>/T-<n>-doubao`` is "ready" iff
    ``DISPATCH-T-<n>.json`` exists AND neither
    ``logs/DONE-T-<n>.txt`` nor ``logs/BLOCKED-T-<n>.txt`` exists.

It then joins that result with the responsibility cards under
``<bridge>/tasks`` to emit one of three verdicts:

* ``WORK_READY`` - at least one worktree passes the scan filter (new mail the
  worker would pick up on its next wake);
* ``IN_REVIEW``  - no new mail, but some card is still non-terminal (work in
  flight or awaiting audit), so the pool is not drained;
* ``DRAINED``    - no new mail and every card is terminal.

``suspect_missing_marker`` flags ready worktrees whose card is already
``awaiting_review``/``auditing``: the delivery happened but the worker omitted
the legacy DONE marker, so the scan produces a false "new mail" signal.

``pending`` is the actionable subset of ``genuine_ready``: a worktree that
passes the scan rule *and* whose responsibility card is still claimable, i.e.
its status is ``dispatched`` or ``retry_requested``.  These are exactly the
tasks the owner has not picked up yet, so they are the ones a wake-up should
mention.  A task whose card already moved on (``claimed`` / ``in_progress`` /
``awaiting_review``) is no longer pending even if the scan rule still fires.
Each pending entry also carries the rework facts when the delivery was
rejected before: ``rework`` (a ``logs/REWORK-<tid>.txt`` note exists),
``rework_reason`` and ``next_attempt`` taken from the dispatch document's
``rework`` block.

Stdlib only, never raises: any read failure is collected in ``errors`` and the
worst case is an ``ERROR`` verdict, so the Mac launcher always gets parseable
JSON to judge by its ``verdict`` field (not the process exit code).
"""
from __future__ import annotations
import os

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_WT_ROOT = Path(os.environ.get("DSH_WT_ROOT", r"C:/Users/32831/wt"))
DEFAULT_BRIDGE_ROOT = Path(os.environ.get("DSH_BRIDGE_ROOT", r"C:/Users/32831/wt/bridge-live"))

# Keep in lockstep with bridge_feeder.IN_FLIGHT_CARD_STATUSES.
NON_TERMINAL_STATUSES = {
    "dispatched", "claimed", "in_progress",
    "awaiting_review", "auditing", "retry_requested",
}
# Already handed to the independent auditor; the worker's part is finished.
REVIEW_STATUSES = {"awaiting_review", "auditing"}
# The owner has not picked the task up yet: responsibility.claim() accepts a
# card in exactly these two states, so these are the tasks worth waking up.
CLAIMABLE_STATUSES = {"dispatched", "retry_requested"}

_WORKTREE_RE = re.compile(r"^T-(\d+)-doubao$")


def _read_json(path: Path, errors: List[str]) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return None


def _read_cards(bridge_root: Path, errors: List[str]) -> Dict[str, Dict[str, Any]]:
    """Map task_id -> compact card view (owner/status). Missing dir => empty."""
    cards: Dict[str, Dict[str, Any]] = {}
    tasks_dir = bridge_root / "tasks"
    if not tasks_dir.is_dir():
        return cards
    for path in sorted(tasks_dir.glob("*.json")):
        data = _read_json(path, errors)
        if not isinstance(data, dict):
            continue
        tid = data.get("task_id") or path.stem
        execution = data.get("execution") or {}
        cards[str(tid)] = {
            "owner": execution.get("owner"),
            "status": execution.get("status"),
            "attempt": execution.get("attempt"),
            "run_id": execution.get("run_id"),
        }
    return cards


def _scan_worktrees(wt_root: Path, errors: List[str]) -> List[Dict[str, Any]]:
    """Apply the SKILL step-1 filter to every T-<n>-doubao worktree."""
    found: List[Dict[str, Any]] = []
    if not wt_root.is_dir():
        errors.append(f"worktree root not found: {wt_root}")
        return found
    for child in sorted(wt_root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        match = _WORKTREE_RE.match(child.name)
        if not match:
            continue
        tid = f"T-{match.group(1)}"
        dispatch = child / f"DISPATCH-{tid}.json"
        done_marker = child / "logs" / f"DONE-{tid}.txt"
        blocked_marker = child / "logs" / f"BLOCKED-{tid}.txt"
        rework_note = child / "logs" / f"REWORK-{tid}.txt"
        has_dispatch = dispatch.is_file()
        ready = has_dispatch and not done_marker.is_file() and not blocked_marker.is_file()

        owner: Optional[str] = None
        rework_block: Optional[Dict[str, Any]] = None
        if has_dispatch:
            doc = _read_json(dispatch, errors)
            if isinstance(doc, dict):
                owner = doc.get("owner")
                block = doc.get("rework")
                if isinstance(block, dict):
                    rework_block = block
        found.append({
            "tid": tid,
            "owner": owner,
            "dispatch": has_dispatch,
            "done": done_marker.is_file(),
            "blocked": blocked_marker.is_file(),
            "ready": ready,
            "rework_note": rework_note.is_file(),
            "rework_reason": (rework_block or {}).get("reason"),
            "next_attempt": (rework_block or {}).get("next_attempt"),
        })
    return found


def _pending(
    ready_rows: List[Dict[str, Any]],
    cards: Dict[str, Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Split the scan-ready rows into claimable work and card-less rows.

    A row is pending when its responsibility card is still claimable, i.e. the
    owner has not claimed it yet.  Rows without a card are reported separately:
    they pass the scan rule but nobody can legally claim them.
    """
    pending: List[Dict[str, Any]] = []
    ready_no_card: List[str] = []
    for row in ready_rows:
        view = cards.get(row["tid"])
        if not view:
            ready_no_card.append(row["tid"])
            continue
        status = view.get("status")
        if status not in CLAIMABLE_STATUSES:
            continue
        pending.append({
            "tid": row["tid"],
            "owner": row.get("owner") or view.get("owner"),
            "status": status,
            "attempt": view.get("attempt"),
            "rework": bool(row.get("rework_note")) or bool(row.get("rework_reason")),
            "rework_reason": row.get("rework_reason"),
            "next_attempt": row.get("next_attempt"),
        })
    return pending, ready_no_card


def probe(wt_root: Path, bridge_root: Path) -> Dict[str, Any]:
    errors: List[str] = []
    worktrees = _scan_worktrees(wt_root, errors)
    cards = _read_cards(bridge_root, errors)

    ready_rows = [w for w in worktrees if w["ready"]]
    suspect = [
        w["tid"] for w in ready_rows
        if cards.get(w["tid"], {}).get("status") in REVIEW_STATUSES
    ]
    ready_tids = [w["tid"] for w in ready_rows]
    genuine_ready = [t for t in ready_tids if t not in suspect]

    pending, ready_no_card = _pending(ready_rows, cards)
    pending_by_owner: Dict[str, List[str]] = {}
    for item in pending:
        pending_by_owner.setdefault(item["owner"] or "UNKNOWN", []).append(item["tid"])

    in_flight: List[Dict[str, Any]] = []
    review_pending: List[Dict[str, Any]] = []
    owner_summary: Dict[str, Dict[str, int]] = {}
    terminal_cards = 0
    for tid, view in sorted(cards.items()):
        status = view.get("status")
        owner = view.get("owner") or "(unassigned)"
        bucket = owner_summary.setdefault(owner, {"in_flight": 0, "review_pending": 0})
        if status in NON_TERMINAL_STATUSES:
            in_flight.append({"tid": tid, "owner": owner, "status": status})
            bucket["in_flight"] += 1
            if status in REVIEW_STATUSES:
                review_pending.append({"tid": tid, "owner": owner, "status": status})
                bucket["review_pending"] += 1
        else:
            terminal_cards += 1

    if ready_tids:
        verdict = "WORK_READY"
    elif in_flight:
        verdict = "IN_REVIEW"
    else:
        verdict = "DRAINED"

    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "verdict": verdict,
        "ts_utc": now.isoformat(),
        "ts_local": now.astimezone().isoformat(),
        "wt_root": str(wt_root),
        "bridge_root": str(bridge_root),
        "worktree_total": len(worktrees),
        "ready": ready_tids,
        "genuine_ready": genuine_ready,
        "suspect_missing_marker": suspect,
        "ready_detail": ready_rows,
        "pending": pending,
        "pending_owners": pending_by_owner,
        "ready_no_card": ready_no_card,
        "in_flight": in_flight,
        "review_pending": review_pending,
        "owner_summary": owner_summary,
        "cards_total": len(cards),
        "terminal_cards": terminal_cards,
        "errors": errors,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Doubao mailbox probe.")
    parser.add_argument("--wt-root", default=str(DEFAULT_WT_ROOT))
    parser.add_argument("--bridge-root", default=str(DEFAULT_BRIDGE_ROOT))
    parser.add_argument("--pretty", action="store_true", help="indent the JSON output")
    args = parser.parse_args(argv)
    try:
        result = probe(Path(args.wt_root), Path(args.bridge_root))
    except Exception as exc:  # noqa: BLE001 - guarantee parseable JSON for the launcher
        result = {
            "verdict": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "ts_utc": datetime.now(timezone.utc).isoformat(),
        }
    if args.pretty:
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        # ASCII-only compact line: safe across the ssh/PowerShell transport.
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
