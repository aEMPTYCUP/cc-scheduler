#!/usr/bin/env python3
"""Minimal Cursor Headless CLI adapter with auto state-machine writeback.

On successful CLI run (returncode 0), automatically transitions task
CLAIMED/IN_PROGRESS → SUBMITTED → REVIEW.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import protocol as p


def build_prompt(package_dir: Path) -> str:
    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    role_spec = (package_dir / "ROLE_SPEC.md").read_text(encoding="utf-8")
    return f"""You are acting as role {manifest['role_id']}.

## Task
{manifest['title']}

{manifest.get('description', '')}

## Acceptance Criteria
{manifest.get('acceptance_criteria', '')}

## ROLE_SPEC (binding, SHA256={manifest['role_spec_sha256']})
{role_spec}

Follow the ROLE_SPEC exactly. Do not invent additional requirements.
"""


def run_cursor_headless(package_dir: Path, extra_prompt: str = "", timeout: int = 600) -> dict:
    prompt = build_prompt(package_dir)
    if extra_prompt:
        prompt += f"\n\n## Additional Instructions\n{extra_prompt}"
    cmd = ["cursor", "-p", "--output-format", "json", prompt]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    except FileNotFoundError:
        return {"error": "cursor CLI not found in PATH", "returncode": -1}
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "returncode": -2}


def auto_submit(task_id: str, actor: str = "cursor-adapter"):
    task = p.get_task(task_id)
    if not task:
        print(f"Task {task_id} not found")
        return
    status = task["status"]
    if status == "CLAIMED":
        p.transition(task_id, "IN_PROGRESS", actor, "adapter started")
        status = "IN_PROGRESS"
    if status == "IN_PROGRESS":
        p.transition(task_id, "SUBMITTED", actor, "adapter completed")
        p.transition(task_id, "REVIEW", "system", "auto-moved to review")
        print(f"Auto-submitted {task_id} → REVIEW")
    else:
        print(f"Task {task_id} in {status}, no auto-submit")


def main():
    parser = argparse.ArgumentParser(description="Cursor headless adapter with auto writeback")
    parser.add_argument("--task-package", required=True, type=Path)
    parser.add_argument("--task-id", help="Task ID for state machine writeback")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--simulate-success", action="store_true", help="Pretend CLI succeeded and auto-submit")
    args = parser.parse_args()

    if not (args.task_package / "manifest.json").exists():
        raise SystemExit(f"Invalid package: {args.task_package}")

    if args.dry_run:
        print(build_prompt(args.task_package))
        return

    result = run_cursor_headless(args.task_package, args.prompt)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    success = args.simulate_success or result.get("returncode") == 0
    if success and args.task_id:
        auto_submit(args.task_id, "cursor-adapter")


if __name__ == "__main__":
    main()
