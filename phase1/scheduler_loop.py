#!/usr/bin/env python3
"""P3 scheduling loop kernel: one deterministic pass over the READY pool.

R-84: check quota -> select_next_task -> atomic_claim -> adapter dispatch ->
recover -> submit (review is triggered by the state machine) -> the human gate
handles integration. The loop itself is deterministic; LLM judgement (contract
generation, UNCERTAIN verdicts, integration) stays out of this module.

Acceptance is NEVER auto-run here: it belongs to the independent reviewer node
(agent-bridge audit launcher). A task that lands in REVIEW waits for that node.
"""
from __future__ import annotations

import argparse
import os
import time
from typing import Callable, Dict, List, Optional

import orchestrator
from orchestrator import WorkerAdapter


def run_preflight(bridge_root: Optional[str] = None,
                  repo_path: Optional[str] = None) -> Dict:
    """Environment preflight (platform_doctor) — fail-closed dispatch gate.

    Every environment defect we hit today was discovered reactively, in the
    middle of a real task. The scheduler therefore refuses to dispatch when the
    platform contracts (bridge mailbox, console DB, git repo, executor CLIs)
    are not healthy. Set DSH_SKIP_PREFLIGHT=1 to bypass (tests, diagnostics).
    """
    if os.environ.get("DSH_SKIP_PREFLIGHT") == "1":
        return {"ok": True, "skipped": True, "failed": [], "checks": []}
    try:
        import platform_doctor
    except Exception as exc:  # noqa: BLE001 - never block on a broken doctor
        return {"ok": True, "skipped": True, "failed": [], "error": str(exc)[:120]}
    argv = ["--skip-executors"]
    if bridge_root:
        argv += ["--bridge-root", bridge_root]
    if repo_path:
        argv += ["--repo", repo_path]
    import io
    import contextlib
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        report = platform_doctor.Report()
        platform_doctor.check_python(report)
        platform_doctor.check_git(report, repo_path)
        platform_doctor.check_stale_worktrees(report, repo_path)
        platform_doctor.check_bridge_mailbox(report, bridge_root or
                                             str(platform_doctor.os.environ.get(
                                                 "DSH_BRIDGE_ROOT",
                                                 r"C:/Users/32831/wt/bridge-live")))
        platform_doctor.check_console_db(report, os.environ.get(
            "PHASE1_DB",
            r"C:/Users/32831/cc-scheduler/phase1/.local/queue.db"))
    failed = [c for c in report.failed()]
    return {
        "ok": not failed,
        "skipped": False,
        "failed": [{"name": c.name, "detail": c.detail, "fix": c.fix} for c in failed],
        "checks": len(report.checks),
    }


def run_schedule_once(
    executor_id: str,
    node: str,
    adapter_factory: Callable[[], WorkerAdapter],
    *,
    max_tasks: int = 1,
    quota_hook: Optional[Callable[[], Optional[str]]] = None,
    preflight: Optional[Callable[[], Dict]] = None,
) -> List[Dict[str, Optional[str]]]:
    """One pass: open a worker session and drive READY tasks through `node`.

    ``max_tasks`` caps how many tasks one pass may process (default 1, the
    conservative production choice). ``quota_hook`` returns None when the
    executor may run, or a pause reason string otherwise. Fail-closed: a hook
    exception pauses the whole pass rather than letting it run unbounded
    (R-87: the quota query must never flip to fail-open).
    """
    if preflight is not None:
        try:
            state = preflight()
        except Exception as exc:  # noqa: BLE001 - fail closed
            state = {"ok": False, "failed": [{"name": "preflight",
                                              "detail": f"{type(exc).__name__}: {exc}",
                                              "fix": "fix the preflight error"}]}
        if not state.get("ok", True):
            return [{"status": "preflight_failed", "task_id": None,
                     "error": "; ".join(f.get("name", "?") + ": " + f.get("detail", "")
                                        for f in state.get("failed", []))[:400]}]

    if quota_hook is not None:
        try:
            pause = quota_hook()
        except Exception as exc:  # noqa: BLE001 - fail-closed pause
            pause = f"quota hook raised {type(exc).__name__}; fail-closed pause"
        if pause:
            return [{"status": "paused", "task_id": None, "error": str(pause)}]

    orchestrator.ensure_orchestrator()
    adapter = adapter_factory()
    # Register whether the active adapter is simulated. The orchestrator refuses
    # to start a worker on a production pool while a simulated adapter is
    # registered (RealPoolSimulationGuard) -- so a real executor must declare
    # itself before start_worker, otherwise the pass can never run in production.
    orchestrator.configure_adapter(bool(getattr(adapter, "is_simulated", False)))
    orchestrator.start_worker(executor_id, node)
    results: List[Dict[str, Optional[str]]] = []
    try:
        for _ in range(max(max_tasks, 1)):
            outcome = orchestrator.run_worker_once(executor_id, node, adapter)
            results.append({
                "status": outcome.get("status"),
                "task_id": outcome.get("task_id"),
                "error": outcome.get("error"),
            })
            if outcome.get("status") in ("idle", "stopped", "blocked", "error"):
                break
    finally:
        orchestrator.stop_worker(executor_id, node)
    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run one deterministic scheduling pass")
    parser.add_argument("--executor", default="codex", help="executor_id (codex/claude/doubao/...)")
    parser.add_argument("--node", default="implement", choices=list(orchestrator.NODES))
    parser.add_argument("--repo", default=None, help="repo path for real adapters")
    parser.add_argument("--max-tasks", type=int, default=1)
    parser.add_argument("--watch-interval", type=int, default=900,
                        help="seconds between passes in --watch mode (default 15 min)")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument("--bridge-root", default=os.environ.get(
        "DSH_BRIDGE_ROOT", r"C:/Users/32831/wt/bridge-live"),
        help="bridge data root (used by the preflight)")
    parser.add_argument("--no-preflight", action="store_true",
                        help="skip the environment preflight (diagnostics only)")
    parser.add_argument("--model", default=None, help="codex model override")
    args = parser.parse_args(argv)

    def factory() -> WorkerAdapter:
        if args.executor == "codex":
            from codex_adapter import CodexAdapter
            from real_adapters import commit_whitelisted
            if not args.repo:
                raise SystemExit("--repo is required for the codex adapter")
            # The Windows sandbox denies .git writes, so the platform commits
            # the whitelisted paths itself after the executor finishes.
            return CodexAdapter(
                args.repo, model=args.model,
                committer=lambda wt, task: commit_whitelisted(
                    wt, task, f"task {task.get('task_id')}: implement via codex"))
        if args.executor == "claude":
            from real_adapters import ClaudeSubprocessAdapter
            if not args.repo:
                raise SystemExit("--repo is required for the claude adapter")
            return ClaudeSubprocessAdapter(args.repo)
        from orchestrator import SimulatedAdapter
        return SimulatedAdapter()

    preflight = (None if args.no_preflight else
                 (lambda: run_preflight(args.bridge_root, args.repo)))

    if args.once:
        for row in run_schedule_once(args.executor, args.node, factory,
                                     max_tasks=args.max_tasks, preflight=preflight):
            print(row)
        return 0
    while True:
        for row in run_schedule_once(args.executor, args.node, factory,
                                     max_tasks=args.max_tasks, preflight=preflight):
            print(row, flush=True)
        time.sleep(max(args.watch_interval, 30))


if __name__ == "__main__":
    raise SystemExit(main())
