#!/usr/bin/env python3
"""Multi-executor orchestration: split one order into subtasks and let
claude / codex / doubao work them cooperatively.

The platform kernel owns state; this module is the planner/dispatcher around
it:

1. ``create_plan`` turns one parent order into a dependency graph of subtasks,
   each bound to an executor channel (tasks.executor_id).
2. ``dispatch`` runs one executor's scheduler pass, so several channels can
   drain the same pool concurrently without competing for the same task
   (atomic_claim enforces the assignment).
3. ``status`` aggregates the subtasks back into the parent.

Nothing here merges or pushes: acceptance and integration stay human gates.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PHASE1_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PHASE1_DIR))

import orchestrator  # noqa: E402
import protocol  # noqa: E402
import scheduler_loop  # noqa: E402

EXECUTORS = ("claude", "codex", "doubao", "cursor")


def create_plan(
    parent_id: str,
    title: str,
    description: str,
    subtasks: List[Dict[str, Any]],
    *,
    role_id: str = "A3-backend-engineer",
    acceptance_criteria: str = "",
    parent_executor: Optional[str] = None,
    created_by: str = "A0-orchestrator",
) -> Dict[str, Any]:
    """Create the parent order plus its subtasks (dependency graph).

    Each subtask accepts: task_id, title, description, executor, depends_on
    (subtask ids), allowed_paths, behavioral_check, pass_condition, priority.

    The parent depends on every subtask, so the parent only becomes eligible
    once all subtasks are DONE.
    """
    known = {parent_id}
    for sub in subtasks:
        tid = str(sub["task_id"]).strip()
        if not tid:
            raise ValueError("every subtask needs a task_id")
        if tid in known:
            raise ValueError(f"duplicate task id in plan: {tid}")
        known.add(tid)

    if protocol.get_task(parent_id) is None:
        protocol.create_task(
            parent_id, title, description, role_id, acceptance_criteria,
            created_by=created_by, priority="high", executor_id=parent_executor,
        )

    created: List[str] = []
    for sub in subtasks:
        tid = str(sub["task_id"]).strip()
        deps = [d for d in (sub.get("depends_on") or []) if d]
        unknown = [d for d in deps if d not in known]
        if unknown:
            raise ValueError(f"{tid} depends on unknown task(s): {unknown}")
        executor = sub.get("executor")
        if executor and executor not in EXECUTORS:
            raise ValueError(f"{tid}: unknown executor {executor!r}")
        protocol.create_task(
            tid,
            sub.get("title") or tid,
            sub.get("description") or "",
            sub.get("role_id") or role_id,
            sub.get("acceptance_criteria") or acceptance_criteria,
            created_by=created_by,
            priority=sub.get("priority") or "medium",
            dependencies=deps,
            allowed_paths=sub.get("allowed_paths") or [],
            behavioral_check=sub.get("behavioral_check") or "",
            pass_condition=sub.get("pass_condition") or "",
            executor_id=executor,
        )
        created.append(tid)

    # The parent waits for every subtask.
    with protocol.get_db() as conn:
        for sub in subtasks:
            conn.execute(
                "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on) VALUES (?, ?)",
                (parent_id, str(sub["task_id"]).strip()),
            )
        conn.commit()

    # Release the subtasks whose own dependencies are already satisfied.
    for sub in subtasks:
        tid = str(sub["task_id"]).strip()
        deps = [d for d in (sub.get("depends_on") or []) if d]
        if all((protocol.get_task(d) or {}).get("status") == "DONE" for d in deps):
            try:
                protocol.ready_task(tid, created_by)
            except ValueError:
                pass
    return {"parent": parent_id, "subtasks": created}


def dispatch(executor_id: str, node: str = "implement", *,
             repo: Optional[str] = None, max_tasks: int = 1,
             preflight: bool = True) -> List[Dict[str, Any]]:
    """Run one executor's pass over the pool (real adapter for claude/codex)."""
    def factory():
        if executor_id == "codex":
            from codex_adapter import CodexAdapter
            from real_adapters import commit_whitelisted
            if not repo:
                raise SystemExit("--repo is required for the codex adapter")
            return CodexAdapter(repo, committer=lambda wt, task: commit_whitelisted(
                wt, task, f"task {task.get('task_id')}: implement via codex"))
        if executor_id == "claude":
            from real_adapters import ClaudeSubprocessAdapter
            if not repo:
                raise SystemExit("--repo is required for the claude adapter")
            return ClaudeSubprocessAdapter(repo)
        if executor_id == "doubao":
            from real_adapters import DoubaoAdapter
            return DoubaoAdapter(bridge_root=os.environ.get(
                "DSH_BRIDGE_ROOT", r"C:/Users/32831/wt/bridge-live"),
                owner=os.environ.get("DSH_DOUBAO_OWNER", "doubao-shenlanking"))
        raise SystemExit(f"no real adapter for executor {executor_id!r}")

    pre = None
    if preflight:
        pre = lambda: scheduler_loop.run_preflight(  # noqa: E731
            os.environ.get("DSH_BRIDGE_ROOT", r"C:/Users/32831/wt/bridge-live"), repo)
    return scheduler_loop.run_schedule_once(
        executor_id, node, factory, max_tasks=max_tasks, preflight=pre)


def status(parent_id: str) -> Dict[str, Any]:
    """Aggregate the subtasks of a parent order."""
    with protocol.get_db() as conn:
        rows = conn.execute(
            "SELECT d.depends_on AS child FROM task_dependencies d "
            "WHERE d.task_id=?", (parent_id,)).fetchall()
    children = []
    for row in rows:
        child = row["child"]
        task = protocol.get_task(child) or {}
        children.append({
            "task_id": child,
            "title": task.get("title"),
            "status": task.get("status"),
            "executor": task.get("executor_id"),
            "implementer": task.get("implementer_executor_id"),
            "verdict": (task.get("audit") or {}).get("verdict") if isinstance(task.get("audit"), dict) else None,
        })
    parent = protocol.get_task(parent_id) or {}
    done = [c for c in children if c["status"] == "DONE"]
    return {
        "parent": parent_id,
        "parent_status": parent.get("status"),
        "subtasks": children,
        "total": len(children),
        "done": len(done),
        "by_executor": {
            ex: sum(1 for c in children if c["executor"] == ex) for ex in EXECUTORS
            if any(c["executor"] == ex for c in children)
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-executor orchestration")
    parser.add_argument("--spec", help="JSON plan file (parent + subtasks)")
    parser.add_argument("--status", help="parent task id: print the aggregated status")
    parser.add_argument("--dispatch", action="append", default=[],
                        help="executor to dispatch (repeatable), e.g. --dispatch claude")
    parser.add_argument("--repo", default=None, help="repository for real adapters")
    parser.add_argument("--max-tasks", type=int, default=1)
    parser.add_argument("--no-preflight", action="store_true")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(status(args.status), ensure_ascii=False, indent=2))
        return 0

    if not args.spec:
        parser.error("--spec or --status is required")
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    plan = create_plan(
        spec["parent_id"], spec.get("title", spec["parent_id"]),
        spec.get("description", ""), spec.get("subtasks", []),
        role_id=spec.get("role_id", "A3-backend-engineer"),
        acceptance_criteria=spec.get("acceptance_criteria", ""),
        parent_executor=spec.get("executor"),
    )
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    for executor in args.dispatch:
        rows = dispatch(executor, "implement", repo=args.repo,
                        max_tasks=args.max_tasks, preflight=not args.no_preflight)
        print(json.dumps({"executor": executor, "rows": rows}, ensure_ascii=False))
    if args.status or args.dispatch:
        print(json.dumps(status(spec["parent_id"]), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
