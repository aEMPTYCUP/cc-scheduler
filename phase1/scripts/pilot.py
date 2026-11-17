#!/usr/bin/env python3
"""Pilot Scenario — human handoff mode (pauses for operator input)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import protocol as p


def pause(msg: str):
    print(f"\n>>> {msg}")
    input("    Press Enter to continue... ")


def run_pilot():
    p.init_db()
    for role in ["A0-orchestrator", "A3-backend-engineer", "A7-code-reviewer", "A8-integration-release"]:
        p.register_role(role)

    tid = "T-PILOT-001"
    p.create_task(
        tid,
        title="Add safe_div function",
        description="Implement safe_div(a, b) that returns a/b or None if b==0. Include unit test.",
        role_id="A3-backend-engineer",
        acceptance_criteria="Correct for defined inputs; has unit test; follows ROLE_SPEC.",
        created_by="A0",
    )
    p.ready_task(tid, "A0")

    pkg = p.atomic_claim(tid, "doubao-operator")
    pkg_dir = Path("task-packages") / tid
    print("\n=== TASK PACKAGE READY ===")
    print(f"Directory : {pkg_dir.resolve()}")
    print(f"ROLE_SPEC : {pkg_dir / 'ROLE_SPEC.md'}")
    print(f"Manifest  : {pkg_dir / 'manifest.json'}")
    print(f"SHA256    : {pkg['manifest']['role_spec_sha256']}")
    print("\nOperator: paste ROLE_SPEC.md + task description into Doubao / Codex.")
    print("Implement with one deliberate, reproducible defect, then return here.")

    pause("After A3 has submitted code + report, continue to mark SUBMITTED")
    p.transition(tid, "IN_PROGRESS", "doubao-operator", "started")
    p.transition(tid, "SUBMITTED", "doubao-operator", "submitted with planted defect")
    p.transition(tid, "REVIEW", "system", "moved to independent review")

    print("\n=== A7 INDEPENDENT REVIEW ===")
    print("Operator: open a fresh Cursor or Claude Code session.")
    print("Paste the task package + submitted code. Do not resume A3 conversation.")
    pause("After A7 reports the defect, continue to mark REWORK")
    p.transition(tid, "REWORK", "A7", "planted defect found", evidence="operator report")

    print("\n=== REWORK ===")
    pause("After A3 fixes the defect and resubmits, continue")
    p.transition(tid, "IN_PROGRESS", "doubao-operator", "fixing")
    p.transition(tid, "SUBMITTED", "doubao-operator", "defect fixed")
    p.transition(tid, "REVIEW", "system", "re-review")

    pause("After A7 confirms fix in a fresh session, continue to ACCEPTED")
    p.transition(tid, "ACCEPTED", "A7", "criteria met")

    print("\n=== A8 INTEGRATION ===")
    pause("After A8 integrates (no business-code change), continue")
    p.transition(tid, "INTEGRATION", "A8", "merging")
    p.transition(tid, "DONE", "A7", "final acceptance")

    print("\n=== PILOT COMPLETE ===")
    print(f"Final status: {p.get_task(tid)['status']}")
    print("Audit trail:")
    for a in p.get_audit(tid):
        print(f"  {a['from_status'] or '-':12} → {a['to_status']:12} {a['actor']}")


if __name__ == "__main__":
    run_pilot()
