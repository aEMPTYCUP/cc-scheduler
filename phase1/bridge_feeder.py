#!/usr/bin/env python3
"""Backlog feeder: keep one claimable task in each Doubao mailbox (backpressure).

The web Doubao accounts are pull workers with no CLI: they scan the bridge
mailbox, work the single card assigned to them, deliver, then look for the next
card. If the mailbox is empty they stop and need a manual restart. This feeder is
the controller-side component that prevents that stall without pre-allocating
the whole pool:

* for each registered Doubao owner it counts in-flight responsibility cards
  (any card not yet at a terminal state);
* when the count drops below ``buffer`` (default 1) and the shared console pool
  has a READY task whose dependencies are done, it atomically claims that task
  for the doubao channel (the row lock makes this mutually exclusive with the
  headless claude worker) and dispatches a bridge card + worktree + DISPATCH;
* terminal cards (needs_human / blocked) release the slot, so the next tick
  refills it. Other READY tasks stay unclaimed and remain available to claude/codex.

All bridge/git side effects are injected, so the decision logic is unit-testable
without importing agent_bridge, running git, or touching a real mailbox.
"""
from __future__ import annotations
import os

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import protocol

# Card statuses that still occupy an owner's slot. Anything else (needs_human,
# blocked) means the account's work on that task has finished and frees the slot.
IN_FLIGHT_CARD_STATUSES = {
    "dispatched", "claimed", "in_progress",
    "awaiting_review", "auditing", "retry_requested",
}
DOUBAO_EXECUTOR = "doubao"
DEFAULT_WORKTREE_ROOT = Path(os.environ.get("DSH_WORKTREE_ROOT", r"C:/Users/32831/wt"))


class FeederError(RuntimeError):
    pass


class BacklogFeeder:
    def __init__(
        self,
        bridge_root: str,
        repo_path: str,
        doubao_owners: List[str],
        *,
        buffer: int = 1,
        card_reader: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        dispatch_hook: Optional[Callable[[Dict[str, Any], str], Dict[str, Any]]] = None,
        max_claim_attempts: int = 5,
        stale_after_seconds: int = 7200,
    ) -> None:
        self.bridge_root = Path(bridge_root)
        self.repo_path = repo_path
        self.doubao_owners = list(doubao_owners)
        self.buffer = max(1, int(buffer))
        self._card_reader = card_reader
        self._dispatch_hook = dispatch_hook
        self.max_claim_attempts = max_claim_attempts
        # Cards nobody moved for this long no longer occupy a mailbox slot.
        self.stale_after_seconds = int(stale_after_seconds)

    # -- bridge side (read-only by default; injectable for tests) ---------- #
    def _read_cards(self) -> List[Dict[str, Any]]:
        if self._card_reader is not None:
            return self._card_reader()
        cards = []
        tasks_dir = self.bridge_root / "tasks"
        if not tasks_dir.is_dir():
            return cards
        for path in sorted(tasks_dir.glob("*.json")):
            try:
                cards.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
        return cards

    @staticmethod
    def _last_activity(card: Dict[str, Any]) -> Optional[float]:
        """Timestamp of the newest recorded card event (mtime as fallback)."""
        stamps = [h.get("created_at") for h in (card.get("history") or [])
                  if h.get("created_at")]
        execution = card.get("execution", {})
        if not stamps and execution.get("claimed_at"):
            stamps = [execution["claimed_at"]]
        if stamps:
            try:
                return datetime.fromisoformat(max(stamps)).timestamp()
            except (TypeError, ValueError):
                pass
        return None

    def _is_stale(self, card: Dict[str, Any]) -> bool:
        """A card nobody has moved for a long time is not really in flight.

        The Doubao accounts are Mac web sessions: when nobody wakes them up, a
        card can sit in "claimed" (or never leave "dispatched") forever. Counting
        such a card as in-flight blocks the mailbox permanently -- the feeder
        would keep skipping while no work happens anywhere.
        """
        if self.stale_after_seconds <= 0:
            return False
        last = self._last_activity(card)
        if last is None:
            # No recorded event at all: fall back to the card file's mtime.
            path = self.bridge_root / "tasks" / f"{card.get('task_id')}.json"
            try:
                last = path.stat().st_mtime
            except OSError:
                return False
        return (time.time() - last) > self.stale_after_seconds

    def count_in_flight(self, owner: str) -> int:
        return sum(
            1
            for card in self._read_cards()
            if card.get("execution", {}).get("owner") == owner
            and card.get("execution", {}).get("status") in IN_FLIGHT_CARD_STATUSES
            and not self._is_stale(card)
        )

    def count_stale(self, owner: str) -> int:
        """In-flight cards that nobody has moved for a long time."""
        return sum(
            1
            for card in self._read_cards()
            if card.get("execution", {}).get("owner") == owner
            and card.get("execution", {}).get("status") in IN_FLIGHT_CARD_STATUSES
            and self._is_stale(card)
        )

    # -- real dispatch side effect (lazy agent_bridge import) -------------- #
    def _default_dispatch(self, task: Dict[str, Any], owner: str) -> Dict[str, Any]:
        bridge_pkg = os.environ.get("DSH_BRIDGE_PKG", "C:/Users/32831/wt/agent-bridge-implementation/.claude")
        import sys
        if bridge_pkg not in sys.path:
            sys.path.insert(0, bridge_pkg)
        import subprocess
        from agent_bridge.dispatcher import (
            build_dispatch_document, dispatch_task, write_dispatch_file,
        )
        from agent_bridge.store import BridgeStore

        tid = task["task_id"]
        worktree = DEFAULT_WORKTREE_ROOT / f"{tid}-doubao"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if worktree.exists():
            if not (worktree / ".git").exists():
                # Legacy plain directory collision: quarantine, never delete.
                worktree.rename(worktree.with_name(
                    f"{worktree.name}.legacy-{int(time.time())}"))
            else:
                pass  # real worktree already present
        if not worktree.exists():
            branch = f"doubao/{tid.lower()}-auto"
            exists = subprocess.run(
                ["git", "-C", self.repo_path, "rev-parse", "--verify",
                 f"refs/heads/{branch}"],
                capture_output=True, text=True).returncode == 0
            if exists:
                # The task was dispatched before (or its branch was created by
                # another lane): attach the existing branch instead of failing
                # on "git worktree add -b" with a branch that already exists.
                made = subprocess.run(
                    ["git", "-C", self.repo_path, "worktree", "add", str(worktree), branch],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
            else:
                made = subprocess.run(
                    ["git", "-C", self.repo_path, "worktree", "add", "-b",
                     branch, str(worktree), "master"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
            if made.returncode != 0:
                raise FeederError(f"worktree add failed: {made.stderr.strip()[:300]}")

        spec = {
            "id": tid,
            "title": task.get("title") or tid,
            "description": task.get("description") or "",
            "allowed_paths": json.loads(task.get("allowed_paths_json") or "[]"),
            "behavioral_check": task.get("behavioral_check") or "",
            "pass_condition": task.get("pass_condition") or "",
        }
        contract_dir = self.bridge_root / "contracts"
        contract_dir.mkdir(parents=True, exist_ok=True)
        contract_path = contract_dir / f"contract.{tid}.json"
        contract_path.write_text(
            json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        store = BridgeStore(self.bridge_root)
        card = dispatch_task(
            store, task_id=tid, owner=owner, worktree=str(worktree),
            branch=f"doubao/{tid.lower()}-auto", contract_path=str(contract_path),
            allowed_paths=spec["allowed_paths"],
            behavioral_check=spec["behavioral_check"],
            pass_condition=spec["pass_condition"], title=spec["title"],
        )
        doc = build_dispatch_document(
            data_root=self.bridge_root, card=card, allowed_paths=spec["allowed_paths"],
            behavioral_check=spec["behavioral_check"], pass_condition=spec["pass_condition"])
        write_dispatch_file(worktree, doc)
        return card

    def _dispatch(self, task: Dict[str, Any], owner: str) -> Dict[str, Any]:
        if self._dispatch_hook is not None:
            return self._dispatch_hook(task, owner)
        return self._default_dispatch(task, owner)

    # -- one refill tick --------------------------------------------------- #
    def activate_ready_tasks(self, limit: int = 20) -> List[str]:
        """Promote BACKLOG tasks whose dependencies are all DONE to READY.

        Without this, a task whose dependencies became satisfied never leaves
        BACKLOG: select_next_task() only considers READY tasks, and in the
        production path nothing else promotes them (the orchestrator seeder
        only runs in demo mode). The feeder is the component that keeps the
        pool moving, so it owns the promotion.
        """
        with protocol.get_db() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT task_id, dependencies_json FROM tasks WHERE status='BACKLOG' "
                "ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
                "created_at")]
        activated: List[str] = []
        for row in rows:
            try:
                deps = json.loads(row.get("dependencies_json") or "[]")
            except json.JSONDecodeError:
                deps = []
            blocked = False
            for dep in deps:
                dep_task = protocol.get_task(dep)
                if dep_task is None or dep_task["status"] != "DONE":
                    blocked = True
                    break
            if blocked:
                continue
            try:
                protocol.ready_task(row["task_id"], actor="feeder")
                activated.append(row["task_id"])
            except Exception:  # noqa: BLE001 - a bad task must not stop the pool
                continue
            if len(activated) >= limit:
                break
        return activated

    def feed_owner(self, owner: str) -> Dict[str, Any]:
        activated = self.activate_ready_tasks()
        in_flight = self.count_in_flight(owner)
        stale = self.count_stale(owner)
        if in_flight >= self.buffer:
            return {"owner": owner, "action": "skipped", "in_flight": in_flight,
                    "stale_cards": stale, "activated": activated}

        attempted = set()
        for _ in range(self.max_claim_attempts):
            task = protocol.select_next_task()
            if task is None:
                return {"owner": owner, "action": "starved",
                        "reason": "no READY task with satisfied dependencies",
                        "activated": activated}
            tid = task["task_id"]
            if tid in attempted:
                break
            attempted.add(tid)
            try:
                # Atomic READY->CLAIMED on the doubao channel; raises if another
                # worker (e.g. headless claude) won the row first.
                protocol.atomic_claim(tid, agent_id=owner, executor_id=DOUBAO_EXECUTOR)
            except ValueError:
                continue  # lost the race; try the next eligible task
            try:
                card = self._dispatch(protocol.get_task(tid), owner)
            except Exception as exc:  # noqa: BLE001 - roll back the console claim
                self._release(tid, owner, exc)
                raise
            return {"owner": owner, "action": "dispatched", "task_id": tid,
                    "card_status": card["execution"]["status"], "in_flight": in_flight + 1,
                    "activated": activated}
        return {"owner": owner, "action": "starved",
                "reason": "all candidate tasks were claimed by another worker",
                "activated": activated}

    @staticmethod
    def _release(tid: str, owner: str, exc: Exception) -> None:
        """Compensate a failed dispatch: release the claim and return to READY."""
        try:
            protocol.release_claim(tid, owner, f"feeder dispatch failed: {type(exc).__name__}")
        except Exception:  # noqa: BLE001 - best-effort audit release
            pass
        try:  # release_claim only marks the claim row; restore READY so the task
              # becomes eligible again instead of being stranded in CLAIMED.
            with protocol.get_db() as conn:
                conn.execute(
                    "UPDATE tasks SET status='READY', claimed_by=NULL, "
                    "implementer_executor_id=NULL, claimed_at=NULL, updated_at=? "
                    "WHERE task_id=? AND status='CLAIMED'",
                    (protocol.utcnow(), tid),
                )
                conn.commit()
        except Exception:  # noqa: BLE001 - never mask the original dispatch error
            pass

    def feed_all(self) -> List[Dict[str, Any]]:
        return [self.feed_owner(owner) for owner in self.doubao_owners]


def _resolve_owners(bridge_root: Path, explicit: Optional[str]) -> List[str]:
    if explicit:
        return [o.strip() for o in explicit.split(",") if o.strip()]
    try:  # default: every active doubao identity registered in the bridge
        import sys
        pkg = "C:/Users/32831/wt/agent-bridge-implementation/.claude"
        if pkg not in sys.path:
            sys.path.insert(0, pkg)
        from agent_bridge.agent_identity import list_agents
        return [a["agent_id"] for a in list_agents(bridge_root)
                if a["kind"] == "doubao" and a["status"] == "active"]
    except Exception:  # noqa: BLE001 - fall back to the two known accounts
        return ["doubao-shenlanking", "doubao-woaijiangjiang"]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Keep one claimable task per Doubao mailbox.")
    parser.add_argument("--bridge-root", default=os.environ.get("DSH_BRIDGE_ROOT", r"C:/Users/32831/wt/bridge-live"))
    parser.add_argument("--repo-path", default=os.environ.get("DSH_REPO_PATH", r"C:/Users/32831/OneDrive/Desktop/期货分析"))
    parser.add_argument("--owners", help="comma-separated doubao ids; default: active registry doubao")
    parser.add_argument("--buffer", type=int, default=1, help="in-flight cards to maintain per owner")
    parser.add_argument("--stale-after", type=int, default=7200,
                        help="秒：一张卡超过这么久无事件即视为僵尸，不再占用邮箱槽位（0=关闭）")
    parser.add_argument("--loop", action="store_true", help="run forever instead of one tick")
    parser.add_argument("--interval", type=float, default=20.0, help="seconds between ticks in --loop")
    args = parser.parse_args(argv)

    owners = _resolve_owners(Path(args.bridge_root), args.owners)
    feeder = BacklogFeeder(args.bridge_root, args.repo_path, owners, buffer=args.buffer,
                           stale_after_seconds=args.stale_after)
    if not args.loop:
        print(json.dumps(feeder.feed_all(), ensure_ascii=False, indent=2))
        return 0
    print(f"feeder loop started: owners={owners} buffer={args.buffer} interval={args.interval}s", flush=True)
    while True:
        try:
            print(json.dumps(feeder.feed_all(), ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the daemon
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False), flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
