#!/usr/bin/env python3
"""Minimal CLI for Phase 1 protocol operations."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import protocol as p


def cmd_init(_):
    p.init_db()
    for role in ["A0-orchestrator", "A3-backend-engineer", "A7-code-reviewer", "A8-integration-release"]:
        try:
            p.register_role(role)
        except FileNotFoundError as e:
            print(f"Skip {role}: {e}")


def cmd_create(args):
    p.create_task(
        task_id=args.task_id,
        title=args.title,
        description=args.description or "",
        role_id=args.role,
        acceptance_criteria=args.acceptance or "",
        created_by=args.actor or "A0",
    )
    p.ready_task(args.task_id, args.actor or "A0")


def cmd_claim(args):
    package = p.atomic_claim(args.task_id, args.agent)
    print(json.dumps(package["manifest"], indent=2, ensure_ascii=False))


def cmd_transition(args):
    p.transition(args.task_id, args.to, args.actor, args.reason or "", args.evidence or "")


def cmd_list(args):
    tasks = p.list_tasks(args.status)
    for t in tasks:
        print(f"{t['task_id']:16} {t['status']:12} {t['role_id']:24} {t['title']}")


def cmd_show(args):
    task = p.get_task(args.task_id)
    if not task:
        print("Not found")
        return
    print(json.dumps(task, indent=2, ensure_ascii=False, default=str))
    print("\n--- Audit ---")
    for a in p.get_audit(args.task_id):
        print(f"{a['created_at']} {a['actor']:12} {a['from_status'] or '-'} → {a['to_status']}  {a['reason']}")


def main():
    parser = argparse.ArgumentParser(description="Phase 1 Protocol CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialize DB and register roles").set_defaults(func=cmd_init)

    c = sub.add_parser("create", help="Create and ready a task")
    c.add_argument("task_id")
    c.add_argument("--title", required=True)
    c.add_argument("--description")
    c.add_argument("--role", required=True)
    c.add_argument("--acceptance")
    c.add_argument("--actor", default="A0")
    c.set_defaults(func=cmd_create)

    c = sub.add_parser("claim", help="Atomically claim a READY task")
    c.add_argument("task_id")
    c.add_argument("--agent", required=True)
    c.set_defaults(func=cmd_claim)

    c = sub.add_parser("transition", help="State transition")
    c.add_argument("task_id")
    c.add_argument("to")
    c.add_argument("--actor", required=True)
    c.add_argument("--reason")
    c.add_argument("--evidence")
    c.set_defaults(func=cmd_transition)

    c = sub.add_parser("list", help="List tasks")
    c.add_argument("--status")
    c.set_defaults(func=cmd_list)

    c = sub.add_parser("show", help="Show task + audit")
    c.add_argument("task_id")
    c.set_defaults(func=cmd_show)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
