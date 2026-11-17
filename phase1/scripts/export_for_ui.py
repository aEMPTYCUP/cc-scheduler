#!/usr/bin/env python3
"""Export protocol-layer tasks and audit log to static JSON for the Phase 1 board.

Reads the SQLite task pool (default: /tmp/phase1_protocol.db) and writes
phase1/ui/data.json plus a data.js wrapper so the board can open from a
file:// page. If the database is missing, writes the built-in demo snapshot.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PHASE1_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path("/tmp/phase1_protocol.db")
DEFAULT_OUT = PHASE1_DIR / "ui" / "data.json"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_task(row: sqlite3.Row) -> dict:
    return {
        "task_id": row["task_id"],
        "title": row["title"],
        "description": row["description"],
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
    return {
        "generated_at": utcnow(),
        "source": f"sqlite:{db_path}",
        "read_only": True,
        "tasks": tasks,
        "audit": audit,
    }


def load_demo() -> dict:
    built_in = PHASE1_DIR / "ui" / "demo_fallback.json"
    if built_in.exists():
        data = json.loads(built_in.read_text(encoding="utf-8"))
        data["generated_at"] = utcnow()
        data["source"] = "demo"
        data["read_only"] = True
        return data
    return {
        "generated_at": utcnow(),
        "source": "demo",
        "read_only": True,
        "tasks": [],
        "audit": [],
    }


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
    print(f"source={payload.get('source')} tasks={n_tasks} audit={n_audit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
