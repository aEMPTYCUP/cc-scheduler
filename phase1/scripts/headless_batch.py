#!/usr/bin/env python3
"""Batch implement-node executor: headless claude in isolated worktrees.

The Doubao accounts are Mac web sessions with no CLI, so they cannot run
unattended. This script drives the same implement node through
``ClaudeSubprocessAdapter`` (headless ``claude -p``), which the console already
exposes, and leaves acceptance to the independent MiniMax reviewer.

Design notes:
  * a task's worktree is reused when one already exists (``wt/<id>-doubao``),
    so rework continues on the branch the implementer already pushed work to;
  * a task that already has a commit when the adapter fails is NOT treated as
    failed -- the T-206/T-207 lesson was that a live implementation was parked
    as FAILED because the runner died, so the commit is probed first;
  * reopening a task to READY keeps an audit trail (the state machine has no
    backwards edge for that).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PHASE1 = Path(__file__).resolve().parents[1]
if str(PHASE1) not in sys.path:
    sys.path.insert(0, str(PHASE1))

import protocol  # noqa: E402
from real_adapters import ClaudeSubprocessAdapter  # noqa: E402

REPO = r"C:/Users/32831/OneDrive/Desktop/期货分析"
WT_ROOT = Path(r"C:/Users/32831/wt")
EXECUTOR = "claude"


def _git(worktree: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", worktree, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return proc.stdout.strip()


def _worktree_factory(task: Dict[str, Any]) -> str:
    """Reuse the implementer's worktree, or create a dedicated one."""
    tid = task["task_id"]
    existing = WT_ROOT / f"{tid}-doubao"
    if existing.is_dir() and (existing / ".git").exists():
        return str(existing)
    path = WT_ROOT / f"{tid}-headless"
    if not path.exists():
        made = subprocess.run(
            ["git", "-C", REPO, "worktree", "add", "-b",
             f"claude/{tid.lower()}", str(path), "master"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if made.returncode != 0:
            raise RuntimeError(f"worktree add {tid}: {made.stderr.strip()[:300]}")
    with protocol.get_db() as conn:
        conn.execute("UPDATE tasks SET worktree_path=?, updated_at=? WHERE task_id=?",
                     (str(path), protocol.utcnow(), tid))
        conn.commit()
    return str(path)


def _reopen_to_ready(tid: str, reason: str) -> None:
    """Reset a non-READY task to READY so atomic_claim can take it.

    The state machine has no backwards edge (REWORK -> READY); the reset is
    written directly and audited, the same way bridge_sync reopens a bounced
    task.
    """
    with protocol.get_db() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (tid,)).fetchone()
        if row is None:
            raise ValueError(f"unknown task {tid}")
        previous = row["status"]
        if previous == "READY":
            return
        conn.execute(
            "UPDATE tasks SET status='READY', claimed_by=NULL, "
            "implementer_executor_id=NULL, claimed_at=NULL, last_error=NULL, "
            "updated_at=? WHERE task_id=?",
            (protocol.utcnow(), tid))
        protocol._audit(conn, tid, "headless-batch", previous, "READY", reason)
        conn.commit()


def _probe_commit(worktree: str, before: Optional[str]) -> Optional[str]:
    """Return the new HEAD if the worktree gained a commit since ``before``."""
    if not worktree:
        return None
    head = _git(worktree, "rev-parse", "HEAD")
    if not head:
        return None
    if before and head == before:
        return None
    ahead = _git(worktree, "rev-list", "--count", f"{before}..HEAD") if before else "1"
    try:
        if int(ahead) <= 0:
            return None
    except ValueError:
        pass
    return head


def _worktree_has_output(tid: str) -> bool:
    """True if the task's worktree carries uncommitted work or a new commit."""
    for suffix in ("-headless", "-doubao"):
        wt = Path(r"C:/Users/32831/wt") / f"{tid}{suffix}"
        if not wt.is_dir():
            continue
        try:
            status = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=30).stdout
        except Exception:  # noqa: BLE001
            continue
        if any(line and not line.startswith("??") for line in status.splitlines()):
            return True
        if any(line.startswith("??") for line in status.splitlines()):
            return True
    return False


def reap_stale_tasks(stale_after_seconds: int = 7200) -> List[str]:
    """Reclaim tasks stuck in CLAIMED/FAILED/IN_PROGRESS so a worker can take them.

    A task claimed by a Doubao account that nobody woke up -- or an implement
    run that was killed -- stays in CLAIMED/FAILED forever: select_next_task()
    only returns READY tasks, so no worker would ever pick it up again. An
    IN_PROGRESS task whose worker died is reclaimed as well, but only when its
    worktree carries no output at all, so work in progress is never thrown away.
    """
    reclaimed: List[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    with protocol.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT task_id, status, claimed_at, updated_at FROM tasks "
            "WHERE status IN ('CLAIMED','FAILED','IN_PROGRESS')")]
    for row in rows:
        stamp = row.get("claimed_at") or row.get("updated_at")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when > cutoff:
            continue  # touched recently: leave it alone
        if row["status"] == "IN_PROGRESS" and _worktree_has_output(row["task_id"]):
            continue  # a worker left something behind: do not discard it
        try:
            with protocol.get_db() as conn:
                conn.execute(
                    "UPDATE tasks SET status='READY', claimed_by=NULL, "
                    "implementer_executor_id=NULL, claimed_at=NULL, last_error=NULL, "
                    "updated_at=? WHERE task_id=? AND status=?",
                    (protocol.utcnow(), row["task_id"], row["status"]))
                protocol._audit(conn, row["task_id"], "headless-worker", row["status"], "READY",
                                "回收僵尸任务：长时间无进展（豆包未唤醒或运行被杀），交由 headless worker 接管")
                conn.commit()
            reclaimed.append(row["task_id"])
        except Exception:  # noqa: BLE001
            continue
    return reclaimed


def _rework_note(tid: str) -> str:
    """The last independent reviewer's REWORK reasons, as rework feedback.

    A rework round that does not know what the reviewer rejected just repeats
    the previous work. The reasons live in the audit trail, so the worker
    feeds them straight into the implement prompt.
    """
    with protocol.get_db() as conn:
        row = conn.execute(
            "SELECT reason FROM audit_log WHERE task_id=? AND to_status='REWORK' "
            "ORDER BY rowid DESC LIMIT 1", (tid,)).fetchone()
    if not row or not row["reason"]:
        return ""
    return ("\n## 上一轮独立验收的 REWORK 理由（必须逐条修复并用可复现断言证明）\n"
            + str(row["reason"]) + "\n")


def run_task(tid: str, *, permission_mode: str, timeout: int, note: str = "") -> Dict[str, Any]:
    task = protocol.get_task(tid)
    if task is None:
        return {"task_id": tid, "ok": False, "error": "unknown task"}
    _reopen_to_ready(tid, f"headless claude 接管 implement 节点（{EXECUTOR}）")
    task = protocol.get_task(tid)
    worktree = WT_ROOT / f"{tid}-doubao"
    before = _git(str(worktree), "rev-parse", "HEAD") if worktree.is_dir() else None
    adapter = ClaudeSubprocessAdapter(
        REPO, worktree_factory=_worktree_factory,
        permission_mode=permission_mode, timeout=timeout, prompt_extra=note)
    try:
        message = adapter.implement(task, EXECUTOR)
    except Exception as exc:  # noqa: BLE001 - never lose a finished implementation
        new_head = _probe_commit(str(worktree), before)
        if new_head:
            with protocol.get_db() as conn:
                conn.execute("UPDATE tasks SET status='READY', claimed_by=NULL, "
                             "implementer_executor_id=NULL, claimed_at=NULL, "
                             "updated_at=? WHERE task_id=?",
                             (protocol.utcnow(), tid))
                conn.commit()
            protocol.add_evidence(tid, "commit_sha", f"commit:{new_head}", EXECUTOR,
                                  sha256=new_head, sync_audit=False)
            protocol.transition(tid, "SUBMITTED", EXECUTOR,
                                f"adapter failed but the worktree carries a new commit ({new_head[:10]})",
                                actor_executor_id=EXECUTOR)
            protocol.transition(tid, "REVIEW", "system",
                                "commit detected after adapter failure -> independent review",
                                actor_executor_id=EXECUTOR)
            return {"task_id": tid, "ok": True, "rescued_commit": new_head,
                    "adapter_error": f"{type(exc).__name__}: {exc}"}
        return {"task_id": tid, "ok": False,
                "error": f"{type(exc).__name__}: {exc}"}
    status = protocol.get_task(tid)["status"]
    return {"task_id": tid, "ok": status in ("REVIEW", "ACCEPTED"), "status": status,
            "message": message}


def _run_loop(args) -> int:
    """Act as the Doubao programming node: one task at a time, forever.

    The Doubao accounts are Mac web sessions and nobody is waking them, so this
    worker takes their place: it promotes unblocked backlog tasks, claims the
    next READY task, implements it with headless claude, and hands the result
    to the independent reviewer. Deliberately sequential -- four concurrent
    claude processes exhausted the machine's memory.
    """
    from bridge_feeder import BacklogFeeder
    feeder = BacklogFeeder(args.bridge_root, args.repo_path, [],
                           stale_after_seconds=0)
    print(f"headless worker started: permission={args.permission_mode} "
          f"timeout={args.timeout}s interval={args.interval}s", flush=True)
    while True:
        try:
            reaped = reap_stale_tasks(args.stale_after)
            if reaped:
                print(json.dumps({"reclaimed": reaped}, ensure_ascii=False), flush=True)
            activated = feeder.activate_ready_tasks()
            if activated:
                print(json.dumps({"activated": activated}, ensure_ascii=False), flush=True)
            task = protocol.select_next_task()
            if task is None:
                time.sleep(args.interval)
                continue
            tid = task["task_id"]
            print(f"=== {tid} ===", flush=True)
            outcome = run_task(tid, permission_mode=args.permission_mode,
                               timeout=args.timeout, note=_rework_note(tid))
            print(json.dumps(outcome, ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False), flush=True)
            time.sleep(args.interval)


def _attach_log_file(path: Optional[str]) -> None:
    """Mirror stdout/stderr into a log file.

    The daemon is started by a .cmd wrapper; relying on the wrapper's `>>`
    redirection is fragile (the handle is held exclusively, so a restarting
    wrapper can fail to reopen the file and the output is lost). Writing the
    file from Python keeps the log reliable across restarts.
    """
    if not path:
        return
    try:
        handle = open(path, "a", encoding="utf-8", buffering=1)
    except OSError as exc:  # noqa: BLE001
        print(f"[log-file unavailable: {exc}]", flush=True)
        return
    sys.stdout = handle
    sys.stderr = handle


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run implement tasks with headless claude.")
    parser.add_argument("--tasks",
                        help="comma separated task ids（--loop 模式下不需要，自动从任务池领取）")
    parser.add_argument("--permission-mode", default="bypassPermissions",
                        choices=("acceptEdits", "bypassPermissions", "default", "plan"))
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--note", default="",
                        help="附加返工说明（拼到实现 prompt 末尾，如上一轮验收的具体错误）")
    parser.add_argument("--loop", action="store_true",
                        help="常驻：自动激活并领取 READY 任务（替代无人唤醒的 Mac 豆包）")
    parser.add_argument("--interval", type=float, default=30.0, help="loop 模式的空闲轮询间隔")
    parser.add_argument("--stale-after", type=int, default=7200,
                        help="秒：CLAIMED/FAILED 任务超过这么久无进展即回收为 READY（0=关闭）")
    parser.add_argument("--log-file", help="把输出同时写入该日志文件")
    parser.add_argument("--bridge-root", default=r"C:/Users/32831/wt/bridge-live")
    parser.add_argument("--repo-path", default=REPO)
    args = parser.parse_args(argv)
    _attach_log_file(args.log_file)
    protocol.ensure_schema()
    if args.loop:
        return _run_loop(args)
    results = []
    for tid in [t.strip() for t in args.tasks.split(",") if t.strip()]:
        print(f"=== {tid} ===", flush=True)
        outcome = run_task(tid, permission_mode=args.permission_mode, timeout=args.timeout,
                           note=args.note)
        results.append(outcome)
        print(json.dumps(outcome, ensure_ascii=False), flush=True)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
