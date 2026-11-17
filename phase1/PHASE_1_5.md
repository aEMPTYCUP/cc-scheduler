# Phase 1.5 — Semi-Assisted Workflow

Phase 1.5 keeps the Phase 1 protocol (Git-backed task pool + SQLite state machine) and adds a human-in-the-loop execution path.

## Workflow

1. The system creates a task, claims it, and writes a task package (`ROLE_SPEC.md`, `manifest.json`).
2. The system prints a complete, copy-paste prompt that already includes the bound ROLE_SPEC and its SHA256.
3. A human pastes that prompt into Doubao or Cursor and does the work.
4. After the operator confirms, the system automatically transitions the task: `CLAIMED → IN_PROGRESS → SUBMITTED → REVIEW`.
5. Independent review (A7) and later integration still follow the Phase 1 state machine.

Start from the `phase1/` directory:

```bash
python3 scripts/assisted_pilot.py \
  --task-id T-ASSIST-001 \
  --role A3-backend-engineer \
  --title "Add safe_div function"
```

Optional flags: `--description`, `--acceptance`, `--agent` (default `doubao-operator`).

## Difference from Phase 1

| | Phase 1 | Phase 1.5 |
| --- | --- | --- |
| Task package and ROLE_SPEC binding | System | System (unchanged) |
| Executor | CLI adapters (`claude_adapter.py`, `cursor_adapter.py`) or the fully manual `pilot.py` | Human executes in Doubao / Cursor from a generated prompt |
| State writeback | Adapter auto-writeback, or every transition entered by hand | After one human confirmation, auto-writeback to `SUBMITTED` then `REVIEW` |
| Quality gate | A7 remains the sole acceptance authority | Same; 1.5 does not skip or replace A7 |

Phase 1.5 is a semi-assisted bridge: the protocol still owns packaging and state, the human owns implementation, and review stays independent.
