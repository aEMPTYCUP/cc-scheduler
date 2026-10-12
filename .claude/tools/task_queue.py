#!/usr/bin/env python3
"""任务队列(降级直连方案,替代 @zsc-glitch/task-queue-mcp)。

队列文件: tasks/queue.json

状态机: pending -> running -> awaiting_review -> passed | failed | needs_human | blocked
        failed -> pending (redo, redo_count += 1)

用法:
    python task_queue.py list [--status pending]
    python task_queue.py next            # 取下一个可执行任务(依赖满足), 标记 running
    python task_queue.py add --title T --description D --priority high \
        --allowed-paths "backend/a.py,backend/b.py" \
        --behavioral-check "python -m pytest backend/tests/test_x.py" \
        --pass-condition "退出码 0"
    python task_queue.py set-status <id> <status> [--result R]
    python task_queue.py redo <id>       # 失败重做, 状态回 pending
    python task_queue.py stats
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

QUEUE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "tasks", "queue.json"
)

VALID_STATUS = {"pending", "running", "awaiting_review", "passed", "failed", "needs_human", "blocked"}
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    if not os.path.exists(QUEUE_FILE):
        return {"tasks": []}
    with open(QUEUE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _next_id(data: dict) -> str:
    existing = {t["id"] for t in data["tasks"]}
    n = 1
    while f"T-{n:03d}" in existing:
        n += 1
    return f"T-{n:03d}"


def cmd_list(args) -> None:
    data = _load()
    tasks = data["tasks"]
    if args.status:
        tasks = [t for t in tasks if t["status"] == args.status]
    if not tasks:
        print("(队列为空)")
        return
    for t in tasks:
        dep = ",".join(t.get("dependencies", [])) or "-"
        print(f"{t['id']} [{t['status']:>15}] {t['priority']:>6} dep={dep}  {t['title']}")


def cmd_next(args) -> None:
    data = _load()
    passed_ids = {t["id"] for t in data["tasks"] if t["status"] == "passed"}
    candidates = [
        t for t in data["tasks"]
        if t["status"] == "pending"
        and all(d in passed_ids for d in t.get("dependencies", []))
    ]
    if not candidates:
        print("{}")
        return
    # 优先级高者先; 同优先级按创建顺序
    candidates.sort(key=lambda t: (PRIORITY_ORDER.get(t["priority"], 9), t["id"]))
    task = candidates[0]
    task["status"] = "running"
    task["started_at"] = _now()
    _save(data)
    print(json.dumps(task, ensure_ascii=False, indent=2))


def cmd_add(args) -> None:
    data = _load()
    task = {
        "id": _next_id(data),
        "title": args.title,
        "description": args.description or "",
        "priority": args.priority,
        "status": "pending",
        "allowed_paths": [p.strip() for p in args.allowed_paths.split(",") if p.strip()],
        "behavioral_check": args.behavioral_check or "",
        "pass_condition": args.pass_condition or "",
        "dependencies": [d.strip() for d in args.dependencies.split(",") if d.strip()],
        "redo_count": 0,
        "created_at": _now(),
        "started_at": None,
        "completed_at": None,
        "result": None,
    }
    data["tasks"].append(task)
    _save(data)
    print(f"已添加 {task['id']}: {task['title']}")


def cmd_set_status(args) -> None:
    data = _load()
    for t in data["tasks"]:
        if t["id"] == args.id:
            if args.status not in VALID_STATUS:
                print(f"非法状态: {args.status}", file=sys.stderr)
                sys.exit(1)
            t["status"] = args.status
            if args.result is not None:
                t["result"] = args.result
            if args.status in {"passed", "failed", "needs_human", "blocked"}:
                t["completed_at"] = _now()
            _save(data)
            print(f"{t['id']} -> {t['status']}")
            return
    print(f"未找到任务 {args.id}", file=sys.stderr)
    sys.exit(1)


def cmd_redo(args) -> None:
    data = _load()
    for t in data["tasks"]:
        if t["id"] == args.id:
            t["status"] = "pending"
            t["redo_count"] = t.get("redo_count", 0) + 1
            t["completed_at"] = None
            _save(data)
            print(f"{t['id']} 重做 (第 {t['redo_count']} 次)")
            return
    print(f"未找到任务 {args.id}", file=sys.stderr)
    sys.exit(1)


def cmd_stats(args) -> None:
    data = _load()
    tasks = data["tasks"]
    by_status: dict[str, int] = {}
    for t in tasks:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    total = len(tasks)
    passed = by_status.get("passed", 0)
    print(f"总任务: {total}")
    print(f"  已完成(通过): {passed}")
    for s in sorted(VALID_STATUS):
        if by_status.get(s):
            print(f"  {s}: {by_status[s]}")
    blockers = [t for t in tasks if t["status"] in {"blocked", "needs_human", "failed"}]
    if blockers:
        print("阻塞/异常任务:")
        for t in blockers:
            print(f"  {t['id']} [{t['status']}] {t['title']}")


def main() -> None:
    p = argparse.ArgumentParser(description="任务队列")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").add_argument("--status", choices=sorted(VALID_STATUS))
    sub.add_parser("next")
    sub.add_parser("stats")

    a = sub.add_parser("add")
    a.add_argument("--title", required=True)
    a.add_argument("--description", default="")
    a.add_argument("--priority", default="medium", choices=["high", "medium", "low"])
    a.add_argument("--allowed-paths", default="")
    a.add_argument("--behavioral-check", default="")
    a.add_argument("--pass-condition", default="")
    a.add_argument("--dependencies", default="")

    s = sub.add_parser("set-status")
    s.add_argument("id")
    s.add_argument("status", choices=sorted(VALID_STATUS))
    s.add_argument("--result", default=None)

    r = sub.add_parser("redo")
    r.add_argument("id")

    args = p.parse_args()
    {
        "list": cmd_list,
        "next": cmd_next,
        "add": cmd_add,
        "set-status": cmd_set_status,
        "redo": cmd_redo,
        "stats": cmd_stats,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
