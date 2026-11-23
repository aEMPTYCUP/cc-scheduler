#!/usr/bin/env python3
"""One-shot importer: bring the formal futures queue into the Phase 1 console.

Reads the authoritative ``tasks/queue.json`` (never writes it), matches every
task to an aa implementation persona through the zero-LLM matcher, provisions
those personas as Phase 1 ``role_specs`` (so role_id is a real foreign key and
the board can build a role-bound prompt), and bulk-loads the tasks with their
structured contract (allowed_paths / behavioral_check / pass_condition /
dependencies). The execution channel (executor_id) is intentionally left NULL:
the persona is the professional *role*, while doubao/codex/claude is assigned
from the console and remains an orthogonal decision.

Status mapping (queue -> Phase 1 state machine):
  pending + dependencies all passed (or no deps) -> READY (board "待编程", claimable)
  pending with unmet dependencies                 -> BACKLOG
  passed / superseded                             -> DONE
  needs_human                                     -> NEEDS_HUMAN
  blocked                                         -> BLOCKED

The import is idempotent under --reset (business tables are rebuilt); the
formal queue.json and its sha256 are never touched.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PHASE1_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PHASE1_DIR / "scripts"
for path in (str(PHASE1_DIR), str(SCRIPTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import protocol  # noqa: E402

DEFAULT_QUEUE = Path(r"C:/Users/32831/OneDrive/Desktop/期货分析/tasks/queue.json")
DEFAULT_AGENT_POOL = Path(r"C:/Users/32831/OneDrive/Desktop/期货分析/.claude/agent-pool")
FALLBACK_ROLE = "manual-triage"
# queue status -> Phase 1 terminal/holding states
_TERMINAL_MAP = {
    "passed": "DONE",
    "superseded": "DONE",
    "needs_human": "NEEDS_HUMAN",
    "blocked": "BLOCKED",
}


def _load_matcher(agent_pool: Path):
    pool_dir = str(agent_pool)
    if pool_dir not in sys.path:
        sys.path.insert(0, pool_dir)
    import matcher  # type: ignore  # lives in the futures agent-pool package
    return matcher


def _provision_roles(agent_pool: Path, used_slugs: List[str]) -> None:
    """Copy each used persona into roles/<slug>/AGENT.md and register it."""
    personas = agent_pool / "personas"
    roles_dir = PHASE1_DIR / "roles"
    # Deterministic fallback role for matcher misses (e.g. empty-signal T-081).
    fallback_dir = roles_dir / FALLBACK_ROLE
    if not fallback_dir.exists():
        fallback_dir.mkdir(parents=True, exist_ok=True)
        (fallback_dir / "AGENT.md").write_text(
            "# 人工分诊（matcher 落空）\n\n"
            "该任务未命中任何实现角色，需人工判断后再指派专业 persona。\n",
            encoding="utf-8",
        )
    for slug in sorted(set(used_slugs)):
        if slug is None:
            continue
        src = personas / f"{slug}.md"
        if not src.is_file():
            raise FileNotFoundError(f"persona missing for matched slug {slug}: {src}")
        dest_dir = roles_dir / slug
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "AGENT.md"
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        protocol.register_role(slug)
    protocol.register_role(FALLBACK_ROLE)


def _map_status(task: Dict[str, Any], id2status: Dict[str, str]) -> str:
    original = task.get("status")
    if original in _TERMINAL_MAP:
        return _TERMINAL_MAP[original]
    if original == "pending":
        deps = task.get("dependencies") or []
        if not deps or all(id2status.get(d) == "passed" for d in deps):
            return "READY"
        return "BACKLOG"
    return "BACKLOG"


def import_queue(queue_path: Path, agent_pool: Path, reset: bool) -> Dict[str, Any]:
    tasks = json.loads(queue_path.read_text(encoding="utf-8"))["tasks"]
    matcher = _load_matcher(agent_pool)
    matches = matcher.match_many(tasks)
    id2status = {t["id"]: t.get("status") for t in tasks}

    used_slugs = [m["slug"] for m in matches if m["slug"]]
    _provision_roles(agent_pool, used_slugs)

    now = protocol.utcnow()
    with protocol.get_db() as conn:
        if reset:
            for table in ("task_evidence", "claims", "task_dependencies", "audit_log", "tasks"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

        existing = {r[0] for r in conn.execute("SELECT task_id FROM tasks").fetchall()}
        status_counter: Dict[str, int] = {}
        role_counter: Dict[str, int] = {}
        skipped = 0

        # Pass 1: task rows (any state), so dependency foreign keys always resolve.
        for task, match in zip(tasks, matches):
            tid = task["id"]
            if tid in existing:
                skipped += 1
                continue
            slug = match["slug"] or FALLBACK_ROLE
            target = _map_status(task, id2status)
            deps = sorted(task.get("dependencies") or [])
            redo = int(task.get("redo_count") or 0)
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, title, description, role_id, status, acceptance_criteria,
                    created_by, created_at, updated_at, claimed_by, claimed_at,
                    priority, dependencies_json, executor_id, implementer_executor_id,
                    retry_count, max_retries, allowed_paths_json, behavioral_check,
                    pass_condition, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, ?, 5, ?, ?, ?, NULL)
                """,
                (
                    tid,
                    task.get("title") or tid,
                    task.get("description") or "",
                    slug,
                    target,
                    task.get("pass_condition") or "",
                    "queue-import",
                    task.get("created_at") or now,
                    now,
                    task.get("priority") or "medium",
                    json.dumps(deps),
                    redo,
                    json.dumps(task.get("allowed_paths") or []),
                    task.get("behavioral_check") or "",
                    task.get("pass_condition") or "",
                ),
            )
            protocol._audit(
                conn, tid, "queue-import", None, target,
                f"imported from queue.json (status={task.get('status')}, "
                f"agent={slug}, reason={match.get('reason', '')[:160]})",
            )
            status_counter[target] = status_counter.get(target, 0) + 1
            role_counter[slug] = role_counter.get(slug, 0) + 1

        # Pass 2: dependency edges (all parent rows now exist).
        for task in tasks:
            for dep in sorted(task.get("dependencies") or []):
                conn.execute(
                    "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on) VALUES (?, ?)",
                    (task["id"], dep),
                )
        conn.commit()

    # A pool built from the authoritative queue is production: the simulated
    # adapter must never auto-run it into fake DONE states.
    protocol.set_pool_mode("production")

    return {
        "total": len(tasks),
        "inserted": len(tasks) - skipped,
        "skipped_existing": skipped,
        "statuses": status_counter,
        "roles": dict(sorted(role_counter.items(), key=lambda kv: -kv[1])),
        "fallback_tasks": [t["id"] for t, m in zip(tasks, matches) if not m["slug"]],
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Import the futures queue into the Phase 1 DB")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--agent-pool", type=Path, default=DEFAULT_AGENT_POOL)
    parser.add_argument("--db", type=Path, required=True, help="Target SQLite DB (PHASE1_DB)")
    parser.add_argument("--reset", action="store_true", help="Rebuild business tables before import")
    args = parser.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    # Redirect the whole protocol layer at this DB for the duration of the run.
    protocol.DB_PATH = args.db.resolve()
    protocol.ensure_schema()

    report = import_queue(args.queue.resolve(), args.agent_pool.resolve(), args.reset)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nDB: {protocol.DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
