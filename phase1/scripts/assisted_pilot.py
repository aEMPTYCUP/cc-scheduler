#!/usr/bin/env python3
"""Phase 1.5 assisted pilot — generate a copy-paste prompt, auto-writeback after human confirm."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import protocol as p

ROLES = [
    "A0-orchestrator",
    "A3-backend-engineer",
    "A7-code-reviewer",
    "A8-integration-release",
]


def pause(msg: str):
    print(f"\n>>> {msg}")
    input("    Press Enter to continue... ")


def build_prompt(package: dict) -> str:
    manifest = package["manifest"]
    role_spec = package["ROLE_SPEC.md"]
    description = manifest.get("description") or ""
    acceptance = manifest.get("acceptance_criteria") or ""
    return f"""You are acting as role {manifest['role_id']}.

## Task
{manifest['title']}

{description}

## Acceptance Criteria
{acceptance}

## ROLE_SPEC (binding, SHA256={manifest['role_spec_sha256']})
{role_spec}

Follow the ROLE_SPEC exactly. Do not invent additional requirements.
When finished, return a brief implementation report that includes role_spec_sha256.
"""


def auto_submit(task_id: str, actor: str):
    task = p.get_task(task_id)
    if not task:
        raise SystemExit(f"Task {task_id} not found")
    status = task["status"]
    if status == "CLAIMED":
        p.transition(task_id, "IN_PROGRESS", actor, "operator started after receiving prompt")
        status = "IN_PROGRESS"
    if status == "IN_PROGRESS":
        p.transition(task_id, "SUBMITTED", actor, "operator confirmed work submitted")
        p.transition(task_id, "REVIEW", "system", "auto-moved to review")
        print(f"Auto-submitted {task_id} → REVIEW")
        return
    raise SystemExit(f"Task {task_id} in {status}, no auto-submit")


def run(args):
    p.init_db()
    for role in ROLES:
        try:
            p.register_role(role)
        except FileNotFoundError as e:
            print(f"Skip {role}: {e}")

    p.create_task(
        task_id=args.task_id,
        title=args.title,
        description=args.description or "",
        role_id=args.role,
        acceptance_criteria=args.acceptance or "",
        created_by="A0",
    )
    p.ready_task(args.task_id, "A0")
    package = p.atomic_claim(args.task_id, args.agent)
    pkg_dir = p.PACKAGES_DIR / args.task_id

    print("\n=== TASK PACKAGE READY ===")
    print(f"Directory : {pkg_dir.resolve()}")
    print(f"ROLE_SPEC : {pkg_dir / 'ROLE_SPEC.md'}")
    print(f"Manifest  : {pkg_dir / 'manifest.json'}")
    print(f"SHA256    : {package['manifest']['role_spec_sha256']}")
    print("\n========== 复制下面内容到豆包/Cursor ==========\n")
    print(build_prompt(package))
    print("========== 以上全部复制 ==========")

    pause("人工执行完成后，按 Enter 确认，系统将自动把任务推进到 SUBMITTED → REVIEW")
    auto_submit(args.task_id, args.agent)
    task = p.get_task(args.task_id)
    print(f"\nFinal status: {task['status']}")


def main():
    parser = argparse.ArgumentParser(
        description="Phase 1.5 assisted pilot: generate a copy-paste prompt, then auto-writeback state"
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--acceptance", default="")
    parser.add_argument("--agent", default="doubao-operator")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
