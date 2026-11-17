#!/usr/bin/env python3
"""Mark a protocol task as SUBMITTED.

If the task is still CLAIMED, it is moved to IN_PROGRESS first, then to
SUBMITTED. Other statuses are rejected. This does not auto-advance to REVIEW.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import protocol as p  # noqa: E402


def mark_submitted(task_id: str, actor: str) -> dict:
    task = p.get_task(task_id)
    if not task:
        raise ValueError(f"Task {task_id} not found")
    status = task["status"]
    if status == "CLAIMED":
        p.transition(task_id, "IN_PROGRESS", actor, "board: start before submit")
        status = "IN_PROGRESS"
    if status == "IN_PROGRESS":
        p.transition(task_id, "SUBMITTED", actor, "board: marked submitted")
        updated = p.get_task(task_id)
        if not updated:
            raise ValueError(f"Task {task_id} missing after transition")
        return updated
    raise ValueError(f"Cannot mark {task_id} as submitted from {status}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move a task to SUBMITTED")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--actor", default="board-operator")
    args = parser.parse_args(argv)
    try:
        task = mark_submitted(args.task_id, args.actor)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"task_id": task["task_id"], "status": task["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
