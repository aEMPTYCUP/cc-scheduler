#!/usr/bin/env python3
"""Orchestration console built on the thin Phase 1 protocol kernel.

This module adds the control plane the read-only board was missing:

* per-executor, per-work-node capability switches (Doubao is coding-only; its
  acceptance switch is locked off and can never be enabled),
* GO/STOP worker sessions with heartbeat and current-task tracking,
* node-aware task selection (the implement node drains READY tasks in
  priority/creation order; the acceptance node drains REVIEW tasks and never
  offers a reviewer their own implementation),
* a pluggable worker loop. The MVP ships :class:`SimulatedAdapter` so the whole
  closed loop runs locally with no remote dependency. The real Doubao and
  claude adapters later implement the same :class:`WorkerAdapter` surface.

It deliberately does not alter ``protocol.py``: every state change still goes
through ``atomic_claim`` / ``transition`` so the legal-edge table, the audit
trail and the self-acceptance guard remain the single source of truth.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import protocol

NODES = protocol.NODES
NODE_LABELS = protocol.NODE_LABELS

# Simulated work pacing: seconds a simulated worker holds a task in IN_PROGRESS
# (and the auditor holds REVIEW) before the next transition. Zero keeps the
# unit tests instant; the console can raise it so tasks visibly flow through
# the node columns instead of completing within one supervisor tick.
_sim_step_delay = 0.0


def set_sim_step_delay(seconds: float) -> float:
    global _sim_step_delay
    _sim_step_delay = max(0.0, float(seconds))
    return _sim_step_delay


def get_sim_step_delay() -> float:
    return _sim_step_delay

# Ordered like the kernel's select_next_task(): priority, then creation time,
# then task id, so two nodes drain their pools in the same deterministic order.
_ORDER = (
    "CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END,"
    " t.created_at ASC, t.task_id ASC"
)


class CapabilityLocked(PermissionError):
    """Raised when a caller tries to toggle a locked (e.g. Doubao audit) switch."""


class WorkerError(RuntimeError):
    """A worker could not proceed (inactive executor, poison task, ...)."""


# --------------------------------------------------------------------------- #
# schema / session bookkeeping
# --------------------------------------------------------------------------- #

def _connect() -> sqlite3.Connection:  # pragma: no cover - thin wrapper
    return protocol.get_db()


def ensure_orchestrator() -> None:
    """Idempotent: migrate the DB and make sure session rows exist."""
    protocol.ensure_schema()
    now = protocol.utcnow()
    with _connect() as conn:
        executors = [r[0] for r in conn.execute("SELECT executor_id FROM executors")]
        conn.executemany(
            "INSERT OR IGNORE INTO worker_sessions "
            "(executor_id, node, running, started_at, stopped_at) VALUES (?, ?, 0, ?, ?)",
            [(executor_id, node, now, now) for executor_id in executors for node in NODES],
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# capability switches
# --------------------------------------------------------------------------- #

def list_capabilities() -> List[Dict[str, Any]]:
    ensure_orchestrator()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT executor_id, node, enabled, locked FROM executor_capabilities "
            "ORDER BY executor_id, node"
        ).fetchall()
        return [dict(r) for r in rows]


def _capability_row(conn: sqlite3.Connection, executor_id: str, node: str):
    return conn.execute(
        "SELECT enabled, locked FROM executor_capabilities "
        "WHERE executor_id=? AND node=?",
        (executor_id, node),
    ).fetchone()


def set_capability(executor_id: str, node: str, enabled: bool) -> Dict[str, Any]:
    """Turn a node switch on/off. Locked rows cannot be changed."""
    if node not in NODES:
        raise ValueError(f"unknown node {node!r}; expected one of {list(NODES)}")
    ensure_orchestrator()
    now = protocol.utcnow()
    with _connect() as conn:
        row = _capability_row(conn, executor_id, node)
        if row is None:
            raise ValueError(f"unknown executor {executor_id!r}")
        if row["locked"]:
            raise CapabilityLocked(
                f"{executor_id}.{node} is locked by policy and cannot be toggled"
            )
        if bool(row["enabled"]) == bool(enabled):
            return {"executor_id": executor_id, "node": node, "enabled": bool(enabled), "changed": False}
        conn.execute(
            "UPDATE executor_capabilities SET enabled=?, updated_at=? "
            "WHERE executor_id=? AND node=?",
            (1 if enabled else 0, now, executor_id, node),
        )
        if not enabled:
            # A switched-off node must not keep claiming work.
            conn.execute(
                "UPDATE worker_sessions SET running=0, stopped_at=? "
                "WHERE executor_id=? AND node=?",
                (now, executor_id, node),
            )
        conn.commit()
    return {"executor_id": executor_id, "node": node, "enabled": bool(enabled), "changed": True}


# --------------------------------------------------------------------------- #
# GO / STP sessions
# --------------------------------------------------------------------------- #

def start_worker(executor_id: str, node: str) -> Dict[str, Any]:
    """GO: mark an (executor, node) session running so the supervisor drives it."""
    if node not in NODES:
        raise ValueError(f"unknown node {node!r}")
    ensure_orchestrator()
    now = protocol.utcnow()
    with _connect() as conn:
        executor = conn.execute(
            "SELECT active FROM executors WHERE executor_id=?", (executor_id,)
        ).fetchone()
        if executor is None:
            raise ValueError(f"unknown executor {executor_id!r}")
        if not executor["active"]:
            raise WorkerError(f"executor {executor_id} is not active")
        cap = _capability_row(conn, executor_id, node)
        if cap is None or not cap["enabled"]:
            raise WorkerError(
                f"{executor_id} has no enabled {node} capability; turn the switch on first"
            )
        conn.execute(
            "INSERT INTO worker_sessions "
            "(executor_id, node, running, current_task_id, last_heartbeat, started_at, stopped_at, last_result) "
            "VALUES (?, ?, 1, NULL, ?, ?, NULL, NULL) "
            "ON CONFLICT(executor_id, node) DO UPDATE SET "
            "running=1, started_at=excluded.started_at, stopped_at=NULL",
            (executor_id, node, now, now),
        )
        conn.commit()
    return {"executor_id": executor_id, "node": node, "running": True}


def stop_worker(executor_id: str, node: str) -> Dict[str, Any]:
    """STOP: no new claims; a task already in flight is allowed to finish."""
    ensure_orchestrator()
    now = protocol.utcnow()
    with _connect() as conn:
        conn.execute(
            "UPDATE worker_sessions SET running=0, stopped_at=? "
            "WHERE executor_id=? AND node=?",
            (now, executor_id, node),
        )
        conn.commit()
    return {"executor_id": executor_id, "node": node, "running": False}


def list_sessions() -> List[Dict[str, Any]]:
    ensure_orchestrator()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT executor_id, node, running, current_task_id, last_heartbeat, "
            "last_result, started_at, stopped_at FROM worker_sessions "
            "ORDER BY executor_id, node"
        ).fetchall()
        return [dict(r) for r in rows]


def _touch(executor_id: str, node: str, **fields: Any) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [executor_id, node]
    with _connect() as conn:
        conn.execute(
            f"UPDATE worker_sessions SET {keys} WHERE executor_id=? AND node=?", values
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# node-aware selection
# --------------------------------------------------------------------------- #

def select_next_task_for_node(node: str, executor_id: str) -> Optional[Dict[str, Any]]:
    """Read-only next eligible task for a node, or None.

    implement  -> READY tasks whose dependencies are all DONE (kernel rule).
    acceptance -> REVIEW tasks the executor did not implement (self-audit guard).
    integrate  -> ACCEPTED tasks waiting for a controlled merge (A8). Only an
                  ACCEPTED task is eligible; the auto loop never reaches merge
                  without the explicit integration node being started.
    A disabled capability yields None before any SQL runs.
    """
    if node not in NODES:
        raise ValueError(f"unknown node {node!r}")
    ensure_orchestrator()
    with _connect() as conn:
        cap = _capability_row(conn, executor_id, node)
        if cap is None or not cap["enabled"]:
            return None
        if node == "implement":
            row = conn.execute(
                f"""
                SELECT t.* FROM tasks t
                WHERE t.status='READY'
                  AND NOT EXISTS (
                    SELECT 1 FROM task_dependencies d
                    JOIN tasks dep ON dep.task_id=d.depends_on
                    WHERE d.task_id=t.task_id AND dep.status <> 'DONE'
                  )
                ORDER BY {_ORDER} LIMIT 1
                """
            ).fetchone()
        elif node == "acceptance":
            row = conn.execute(
                f"""
                SELECT t.* FROM tasks t
                WHERE t.status='REVIEW'
                  AND (t.implementer_executor_id IS NULL OR t.implementer_executor_id <> ?)
                  AND (t.claimed_by IS NULL OR t.claimed_by <> ?)
                ORDER BY {_ORDER} LIMIT 1
                """,
                (executor_id, executor_id),
            ).fetchone()
        else:  # integrate
            row = conn.execute(
                f"""
                SELECT t.* FROM tasks t
                WHERE t.status='ACCEPTED'
                ORDER BY {_ORDER} LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None


# --------------------------------------------------------------------------- #
# worker adapters
# --------------------------------------------------------------------------- #

class WorkerAdapter:
    """Interface real executors implement. Methods drive the kernel state machine."""

    def implement(self, task: Dict[str, Any], executor_id: str) -> str:
        raise NotImplementedError

    def accept(self, task: Dict[str, Any], executor_id: str) -> str:
        raise NotImplementedError

    def integrate(self, task: Dict[str, Any], executor_id: str) -> str:
        raise NotImplementedError


class SimulatedAdapter(WorkerAdapter):
    """No external dependency: deterministic, instant, audit-trail friendly.

    acceptance_verdict chooses the REVIEW decision so tests/demos can exercise
    ACCEPTED / REWORK / NEEDS_HUMAN without a real claude subprocess.
    """

    def __init__(self, acceptance_verdict: str = "ACCEPTED",
                 step_delay: Optional[float] = None) -> None:
        if acceptance_verdict not in ("ACCEPTED", "REWORK", "NEEDS_HUMAN"):
            raise ValueError("acceptance_verdict must be ACCEPTED, REWORK or NEEDS_HUMAN")
        self.acceptance_verdict = acceptance_verdict
        # None -> read the process-wide pacing each time, so a console change
        # reaches the supervisor's freshly-constructed adapter on its next tick.
        self.step_delay = get_sim_step_delay() if step_delay is None else max(0.0, float(step_delay))

    def implement(self, task: Dict[str, Any], executor_id: str) -> str:
        tid = task["task_id"]
        protocol.atomic_claim(tid, agent_id=executor_id, executor_id=executor_id)
        protocol.transition(tid, "IN_PROGRESS", executor_id,
                            "sim: start implementation", actor_executor_id=executor_id)
        if self.step_delay:
            time.sleep(self.step_delay)  # visibly linger in the "coding" column
        protocol.transition(tid, "SUBMITTED", executor_id,
                            "sim: implementation finished", actor_executor_id=executor_id)
        protocol.transition(tid, "REVIEW", executor_id,
                            "sim: submitted for independent acceptance",
                            actor_executor_id=executor_id)
        protocol.add_evidence(tid, "other", f"sim://implement/{executor_id}",
                              executor_id, sync_audit=False)
        return f"{tid} implemented and moved to REVIEW"

    def accept(self, task: Dict[str, Any], executor_id: str) -> str:
        tid = task["task_id"]
        if self.step_delay:
            time.sleep(self.step_delay)  # visibly linger in the "review" column
        protocol.transition(tid, self.acceptance_verdict, executor_id,
                            f"sim: independent acceptance -> {self.acceptance_verdict}",
                            actor_executor_id=executor_id)
        return f"{tid} acceptance decision {self.acceptance_verdict}"

    def integrate(self, task: Dict[str, Any], executor_id: str) -> str:
        """State-only integration for the demo (no real git in the simulation)."""
        tid = task["task_id"]
        if self.step_delay:
            time.sleep(self.step_delay)
        protocol.transition(tid, "INTEGRATION", executor_id,
                            "sim: begin controlled integration",
                            actor_executor_id=executor_id)
        protocol.transition(tid, "DONE", executor_id,
                            "sim: integration complete", actor_executor_id=executor_id)
        return f"{tid} integrated (simulated) and moved to DONE"


# --------------------------------------------------------------------------- #
# read-only merge preview (no ref / index / worktree mutation)
# --------------------------------------------------------------------------- #

def _git_read(repo_path: str, *args: str) -> "subprocess.CompletedProcess":
    return subprocess.run(
        ["git", "-C", str(repo_path), *args], capture_output=True, text=True,
    )


def _parse_merge_tree(returncode: int, output: str) -> tuple[bool, List[str]]:
    """Parse `git merge-tree --write-tree --name-only` output into (conflict, files)."""
    if returncode == 0:
        return False, []
    files: List[str] = []
    for line in output.splitlines()[1:]:  # line 1 is the merged tree OID
        stripped = line.strip()
        if not stripped or re.fullmatch(r"[0-9a-f]{40}", stripped):
            continue
        if stripped.startswith("Auto-merging"):
            continue
        match = re.match(r"CONFLICT .*? in (.+)$", stripped)
        if match:
            files.append(match.group(1).strip())
        elif not stripped.startswith("CONFLICT"):
            files.append(stripped)  # bare conflicted path from --name-only
    seen: set[str] = set()
    unique = [p for p in files if not (p in seen or seen.add(p))]
    return True, unique


def analyze_merge(repo_path: str, source_branch: str,
                  target_branch: str = "master") -> Dict[str, Any]:
    """Read-only mergeability of source_branch into target_branch.

    Uses rev-parse / merge-base / merge-tree / diff, none of which move a ref,
    touch the index or change the working tree (merge-tree only writes a tree
    object). Mirrors the old engine's "master advanced -> rebase" rule with the
    ``up_to_date`` flag instead of forcing any write.
    """
    repo = str(repo_path)

    def head(branch: str) -> str:
        result = _git_read(repo, "rev-parse", "--verify", f"{branch}^{{commit}}")
        if result.returncode != 0:
            raise ValueError(f"unknown branch {branch!r}: {result.stderr.strip()[:200]}")
        return result.stdout.strip()

    target_head = head(target_branch)
    source_head = head(source_branch)
    base = _git_read(repo, "merge-base", target_branch, source_branch)
    merge_base = base.stdout.strip() if base.returncode == 0 else ""

    ancestor = _git_read(repo, "merge-base", "--is-ancestor", target_branch, source_branch)
    up_to_date = ancestor.returncode == 0  # target fully contained in source

    tree = _git_read(repo, "merge-tree", "--write-tree", "--name-only",
                     target_branch, source_branch)
    conflicts, conflict_files = _parse_merge_tree(tree.returncode, tree.stdout)

    changed: List[str] = []
    if merge_base:
        diff = _git_read(repo, "diff", "--name-only", f"{merge_base}...{source_branch}")
        if diff.returncode == 0:
            changed = [line for line in diff.stdout.splitlines() if line]

    if conflicts:
        verdict = "conflict"
    elif up_to_date:
        verdict = "fast_forward"
    else:
        verdict = "merge_needed"  # clean 3-way, but target advanced -> rebase expected
    return {
        "repo_path": repo,
        "target_branch": target_branch,
        "source_branch": source_branch,
        "target_head": target_head,
        "source_head": source_head,
        "merge_base": merge_base,
        "up_to_date": up_to_date,
        "fast_forwardable": up_to_date and not conflicts,
        "needs_rebase": not up_to_date,
        "conflicts": conflicts,
        "conflict_files": conflict_files,
        "changed_files": changed,
        "verdict": verdict,
        "writable": False,  # this analysis never performs a write
    }


class GitIntegrationAdapter(WorkerAdapter):
    """Real A8 integration: merge an accepted task branch into the target branch.

    Unlike the simulation this runs real, local, reversible git operations. It
    never pushes and never touches the formal queue. On a clean fast-forwardable
    merge it runs the optional post-merge check, records the merge commit as
    evidence and drives ACCEPTED -> INTEGRATION -> DONE. On a conflict or a
    failing post-merge check it aborts the merge and returns the task to REVIEW
    (the state machine's INTEGRATION -> REVIEW edge) with evidence instead of
    silently fixing business code.

    :meth:`preview` is a strictly read-only mergeability analysis built on
    ``git merge-tree``: it writes no ref, index entry or working-tree file and
    is the only method allowed before the adapter is armed for real writes.
    """

    def __init__(self, repo_path: str, target_branch: str = "main",
                 check_command: str = "", remote_push: bool = False) -> None:
        self.repo_path = str(repo_path)
        self.target_branch = target_branch
        self.check_command = check_command
        self.remote_push = remote_push  # reserved; pushing stays explicitly off

    def _git(self, *args: str) -> "subprocess.CompletedProcess":
        return subprocess.run(
            ["git", "-C", self.repo_path, *args],
            capture_output=True, text=True,
        )

    @staticmethod
    def _branch_of(worktree: str) -> str:
        result = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        branch = result.stdout.strip()
        if result.returncode != 0 or not branch or branch == "HEAD":
            raise ValueError(f"worktree {worktree} is not on a named branch")
        return branch

    def preview(self, source_branch: Optional[str] = None,
                task: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Read-only mergeability check; never mutates refs, index or worktree."""
        branch = source_branch or self._branch_of(task["worktree_path"])
        return analyze_merge(self.repo_path, branch, self.target_branch)

    def implement(self, task, executor_id: str) -> str:
        raise PermissionError("the integration adapter cannot implement tasks")

    def accept(self, task, executor_id: str) -> str:
        raise PermissionError("the integration adapter cannot accept tasks")

    def integrate(self, task: Dict[str, Any], executor_id: str) -> str:
        tid = task["task_id"]
        worktree = task.get("worktree_path")
        if not worktree:
            raise ValueError(f"task {tid} has no worktree_path; nothing real to merge")
        branch = self._branch_of(worktree)

        protocol.transition(tid, "INTEGRATION", executor_id,
                            f"A8: merging {branch} into {self.target_branch}",
                            actor_executor_id=executor_id)
        try:
            self._git("checkout", self.target_branch)
            merge = self._git("merge", "--no-ff", branch, "-m",
                              f"Merge task {tid} ({branch}) into {self.target_branch}")
            if merge.returncode != 0:
                self._git("merge", "--abort")
                self._send_back_to_review(tid, executor_id,
                                          f"merge conflict: {merge.stderr.strip()[:300]}")
                return f"{tid} conflict -> sent back to REVIEW"
            if self.check_command:
                check = subprocess.run(self.check_command, cwd=self.repo_path,
                                       shell=True, capture_output=True, text=True)
                if check.returncode != 0:
                    self._git("reset", "--hard", "ORIG_HEAD")
                    self._send_back_to_review(
                        tid, executor_id,
                        f"post-merge check failed rc={check.returncode}: "
                        f"{check.stdout[-200:]} {check.stderr[-200:]}")
                    return f"{tid} failed checks -> sent back to REVIEW"
            head = self._git("rev-parse", "HEAD").stdout.strip()
            protocol.add_evidence(tid, "commit_sha", head, executor_id,
                                  sha256=head, sync_audit=False)
            protocol.transition(tid, "DONE", executor_id,
                                f"A8: merged {branch} -> {self.target_branch} ({head[:10]})",
                                evidence_path=f"commit:{head}",
                                actor_executor_id=executor_id)
            return f"{tid} merged into {self.target_branch} as {head[:10]} -> DONE"
        except Exception as exc:  # noqa: BLE001 - never leave a half-merged tree
            self._git("merge", "--abort")
            self._send_back_to_review(tid, executor_id, f"integration error: {exc}")
            return f"{tid} integration error -> sent back to REVIEW"

    def _send_back_to_review(self, tid: str, executor_id: str, reason: str) -> None:
        # INTEGRATION -> REVIEW is a legal edge; re-review is required before retry.
        task = protocol.get_task(tid)
        if task and task["status"] == "INTEGRATION":
            protocol.transition(tid, "REVIEW", executor_id, reason,
                                actor_executor_id=executor_id)
        else:
            protocol.block_task(tid, executor_id, reason)


# --------------------------------------------------------------------------- #
# worker loop
# --------------------------------------------------------------------------- #

def run_worker_once(executor_id: str, node: str,
                    adapter: Optional[WorkerAdapter] = None) -> Dict[str, Any]:
    """Process at most one task. Returns a status dict; never raises for task errors."""
    adapter = adapter or SimulatedAdapter()
    ensure_orchestrator()
    with _connect() as conn:
        session = conn.execute(
            "SELECT running FROM worker_sessions WHERE executor_id=? AND node=?",
            (executor_id, node),
        ).fetchone()
    if session is None or not session["running"]:
        return {"status": "stopped", "executor_id": executor_id, "node": node}

    task = select_next_task_for_node(node, executor_id)
    if task is None:
        _touch(executor_id, node, last_heartbeat=protocol.utcnow(),
               current_task_id=None, last_result="idle: no eligible task")
        return {"status": "idle", "executor_id": executor_id, "node": node}

    tid = task["task_id"]
    _touch(executor_id, node, current_task_id=tid, last_heartbeat=protocol.utcnow())
    try:
        if node == "implement":
            summary = adapter.implement(task, executor_id)
        elif node == "acceptance":
            summary = adapter.accept(task, executor_id)
        else:  # integrate
            summary = adapter.integrate(task, executor_id)
    except PermissionError as exc:  # self-acceptance backstop / policy refusal
        _touch(executor_id, node, current_task_id=None,
               last_heartbeat=protocol.utcnow(), last_result=f"blocked: {exc}")
        return {"status": "blocked", "task_id": tid, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - contain a poison task, stop the loop
        _touch(executor_id, node, running=0, current_task_id=None,
               stopped_at=protocol.utcnow(), last_result=f"error: {type(exc).__name__}: {exc}")
        return {"status": "error", "task_id": tid, "error": str(exc)}

    _touch(executor_id, node, current_task_id=None,
           last_heartbeat=protocol.utcnow(), last_result=summary)
    return {"status": "worked", "task_id": tid, "summary": summary}


def run_worker_drain(executor_id: str, node: str,
                     adapter: Optional[WorkerAdapter] = None,
                     max_tasks: int = 100) -> List[Dict[str, Any]]:
    """Drive a running session until its pool is empty/stopped (tests, on-demand)."""
    out: List[Dict[str, Any]] = []
    for _ in range(max_tasks):
        result = run_worker_once(executor_id, node, adapter)
        out.append(result)
        if result["status"] != "worked":
            break
    return out


class WorkerSupervisor:
    """Single daemon thread that ticks every running session.

    One thread (not one per agent) sequentially calls run_worker_once for each
    running (executor, node). Each protocol call opens its own short-lived
    SQLite connection, so no connection crosses thread boundaries.
    """

    def __init__(self, adapter_factory: Callable[[], WorkerAdapter] = SimulatedAdapter,
                 interval: float = 1.5) -> None:
        self.adapter_factory = adapter_factory
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ccs-worker-supervisor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                ensure_orchestrator()
                for session in list_sessions():
                    if not session["running"]:
                        continue
                    run_worker_once(session["executor_id"], session["node"],
                                    self.adapter_factory())
            except Exception:  # noqa: BLE001 - supervisor must never die on one bad tick
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# human integration + console snapshot
# --------------------------------------------------------------------------- #

def integrate_task(task_id: str, actor: str = "human") -> Dict[str, Any]:
    """Manual ACCEPTED -> INTEGRATION -> DONE. Auto loops never integrate."""
    task = protocol.get_task(task_id)
    if not task:
        raise ValueError(f"task {task_id} not found")
    if task["status"] != "ACCEPTED":
        raise ValueError(f"only ACCEPTED tasks can integrate, got {task['status']}")
    protocol.transition(task_id, "INTEGRATION", actor, "human: begin controlled integration")
    protocol.transition(task_id, "DONE", actor, "human: integration complete")
    integrated = protocol.get_task(task_id)
    return integrated


def console_state() -> Dict[str, Any]:
    """Everything the console needs in one read."""
    ensure_orchestrator()
    with _connect() as conn:
        executors = [dict(r) for r in conn.execute(
            "SELECT executor_id, label, kind, endpoint_env, active FROM executors ORDER BY executor_id"
        )]
        caps = [dict(r) for r in conn.execute(
            "SELECT executor_id, node, enabled, locked FROM executor_capabilities"
        )]
        sessions = [dict(r) for r in conn.execute(
            "SELECT executor_id, node, running, current_task_id, last_heartbeat, "
            "last_result, started_at, stopped_at FROM worker_sessions"
        )]
    cap_map: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for cap in caps:
        cap_map.setdefault(cap["executor_id"], {})[cap["node"]] = cap
    sess_map: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for sess in sessions:
        sess_map.setdefault(sess["executor_id"], {})[sess["node"]] = sess
    for executor in executors:
        executor["capabilities"] = cap_map.get(executor["executor_id"], {})
        executor["sessions"] = sess_map.get(executor["executor_id"], {})
    return {
        "nodes": [{"id": node, "label": NODE_LABELS[node]} for node in NODES],
        "executors": executors,
        "sim_step_delay": get_sim_step_delay(),
    }


def _ensure_role(role_id: str) -> None:
    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM role_specs WHERE role_id=?", (role_id,)
        ).fetchone()
    if not exists:
        protocol.register_role(role_id)


def seed_demo_pool(role_id: str = "A3-backend-engineer") -> Dict[str, int]:
    """Reset the pool to a reproducible demo state and stop every worker.

    Produces several READY coding tasks across priorities plus one task already
    in REVIEW (implemented by Doubao) so the acceptance node has immediate work.
    """
    ensure_orchestrator()
    _ensure_role(role_id)
    with _connect() as conn:
        conn.execute("UPDATE worker_sessions SET running=0, current_task_id=NULL")
        for table in ("task_evidence", "claims", "task_dependencies", "audit_log", "tasks"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()

    ready_specs = [
        ("T-POOL-01", "高优先：修复登录回调", "high"),
        ("T-POOL-02", "高优先：导出 CSV 空值处理", "high"),
        ("T-POOL-03", "中优先：看板节点徽标", "medium"),
        ("T-POOL-04", "中优先：审计流时间格式化", "medium"),
        ("T-POOL-05", "低优先：文档错别字", "low"),
    ]
    for task_id, title, priority in ready_specs:
        protocol.create_task(task_id, title, f"模拟任务 {title}", role_id,
                             f"验收 {title}", priority=priority)
        protocol.ready_task(task_id, actor="seeder")

    # One task already implemented by Doubao and parked in REVIEW for claude.
    protocol.create_task("T-AUDIT-01", "待验收：豆包已完成的接口", "模拟验收任务",
                         role_id, "独立验收该接口", priority="high")
    protocol.ready_task("T-AUDIT-01", actor="seeder")
    protocol.atomic_claim("T-AUDIT-01", agent_id="doubao", executor_id="doubao")
    protocol.transition("T-AUDIT-01", "IN_PROGRESS", "doubao", actor_executor_id="doubao")
    protocol.transition("T-AUDIT-01", "SUBMITTED", "doubao", actor_executor_id="doubao")
    protocol.transition("T-AUDIT-01", "REVIEW", "doubao", actor_executor_id="doubao")

    counts = {"READY": len(ready_specs), "REVIEW": 1}
    return counts
