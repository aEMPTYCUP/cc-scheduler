#!/usr/bin/env python3
"""Export protocol-layer tasks and audit log to static JSON for the Phase 1 board.

Reads the SQLite task pool (default: /tmp/phase1_protocol.db) and writes
phase1/ui/data.json plus a data.js wrapper so the board can open from a
file:// page. If the database is missing, writes the built-in demo snapshot.

The snapshot includes role-spec summaries so the board can build a
copy-paste prompt without calling a live backend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PHASE1_DIR = Path(__file__).resolve().parents[1]
ROLES_DIR = PHASE1_DIR / "roles"
DEFAULT_DB = Path("/tmp/phase1_protocol.db")
DEFAULT_OUT = PHASE1_DIR / "ui" / "data.json"
SUBMITTABLE = {"CLAIMED", "IN_PROGRESS"}
EXECUTORS = [
    {"id": "doubao", "label": "豆包"},
    {"id": "codex", "label": "Codex"},
    {"id": "claude", "label": "Claude"},
    {"id": "cursor", "label": "Cursor"},
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_task(row: sqlite3.Row) -> dict:
    keys = set(row.keys())
    return {
        "task_id": row["task_id"],
        "title": row["title"],
        "description": row["description"],
        "acceptance_criteria": row["acceptance_criteria"] if "acceptance_criteria" in keys else "",
        "role_id": row["role_id"],
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "claimed_by": row["claimed_by"],
        "claimed_at": row["claimed_at"],
        "worktree_path": row["worktree_path"],
    }


def row_audit(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "actor": row["actor"],
        "from_status": row["from_status"],
        "to_status": row["to_status"],
        "reason": row["reason"] or "",
        "created_at": row["created_at"],
    }


def summarize_role(content: str) -> str:
    """Keep the title plus Purpose / Responsibilities / Non-Responsibilities."""
    lines = content.splitlines()
    kept: list[str] = []
    section_count = 0
    for line in lines:
        if line.startswith("## "):
            section_count += 1
            if section_count > 3:
                break
        kept.append(line)
    text = "\n".join(kept).strip()
    if len(text) < 80:
        text = "\n".join(lines[:40]).strip()
    return text


def load_roles() -> dict:
    roles: dict[str, dict] = {}
    if not ROLES_DIR.exists():
        return roles
    for path in sorted(ROLES_DIR.glob("*/AGENT.md")):
        role_id = path.parent.name
        content = path.read_text(encoding="utf-8")
        roles[role_id] = {
            "role_id": role_id,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "summary": summarize_role(content),
        }
    return roles


def build_prompt_base(task: dict, role: dict | None) -> str:
    description = (task.get("description") or "").strip() or "（没有补充说明）"
    acceptance = (task.get("acceptance_criteria") or "").strip() or "（未提供验收标准）"
    role_id = task.get("role_id") or "未指定角色"
    if role:
        spec = (
            f"## ROLE_SPEC 摘要（绑定，SHA256={role['sha256']}）\n"
            f"{role['summary']}"
        )
    else:
        spec = f"## ROLE_SPEC 摘要\n未找到角色 {role_id} 的 ROLE_SPEC，请先在 roles/ 下补齐 AGENT.md。"
    return (
        f"你现在的角色是 {role_id}。\n\n"
        f"## 任务\n"
        f"{task.get('task_id', '')}  {task.get('title') or '未命名任务'}\n\n"
        f"{description}\n\n"
        f"## 验收标准\n"
        f"{acceptance}\n\n"
        f"{spec}\n\n"
        "请严格按 ROLE_SPEC 执行，不要自行增加需求。"
        "完成后请给出简要实现说明，并写明 role_spec_sha256。"
    )


def enrich(payload: dict) -> dict:
    roles = load_roles()
    tasks = list(payload.get("tasks") or [])
    audit = list(payload.get("audit") or [])
    latest: dict[str, dict] = {}
    for item in audit:
        latest[item["task_id"]] = item
    for task in tasks:
        task.setdefault("acceptance_criteria", "")
        task["latest_audit"] = latest.get(task["task_id"])
        task["can_submit"] = task.get("status") in SUBMITTABLE
        role = roles.get(task.get("role_id") or "")
        task["prompt_base"] = build_prompt_base(task, role)
    payload["tasks"] = tasks
    payload["audit"] = audit
    payload["roles"] = roles
    payload["executors"] = EXECUTORS
    payload["read_only"] = True
    return payload


def load_from_db(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tasks = [
            row_task(r)
            for r in conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        ]
        audit = [
            row_audit(r)
            for r in conn.execute(
                "SELECT id, task_id, actor, from_status, to_status, reason, created_at "
                "FROM audit_log ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()
    return enrich(
        {
            "generated_at": utcnow(),
            "source": f"sqlite:{db_path}",
            "read_only": True,
            "tasks": tasks,
            "audit": audit,
        }
    )


def load_demo() -> dict:
    built_in = PHASE1_DIR / "ui" / "demo_fallback.json"
    if built_in.exists():
        data = json.loads(built_in.read_text(encoding="utf-8"))
    else:
        data = {"tasks": [], "audit": []}
    data["generated_at"] = utcnow()
    data["source"] = "demo"
    data["read_only"] = True
    return enrich(data)


def write_payload(payload: dict, out_json: Path) -> None:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    out_json.write_text(text, encoding="utf-8")
    out_js = out_json.with_suffix(".js")
    out_js.write_text("window.PHASE1_DATA = " + text.rstrip() + ";\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export protocol tasks for the static board")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output JSON path")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Write the demo snapshot even if a database exists",
    )
    args = parser.parse_args(argv)

    if args.demo or not args.db.exists():
        payload = load_demo()
        payload["source"] = "demo"
    else:
        try:
            payload = load_from_db(args.db)
        except sqlite3.Error as exc:
            print(f"Failed to read {args.db}: {exc}", file=sys.stderr)
            return 1

    write_payload(payload, args.out)
    n_tasks = len(payload.get("tasks") or [])
    n_audit = len(payload.get("audit") or [])
    print(f"Wrote {args.out} and {args.out.with_suffix('.js')}")
    print(f"source={payload.get('source')} tasks={n_tasks} audit={n_audit} roles={len(payload.get('roles') or {})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
