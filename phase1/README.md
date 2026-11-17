# Phase 1 Protocol Layer

Thin Git-backed task pool + SQLite state machine for multi-agent development factory.

See CI_INTEGRATION.md and LANDING_CHECKLIST.md for deployment steps.

## Phase 1.5 (semi-assisted)

The system creates the task package and a copy-paste prompt that includes the bound ROLE_SPEC. A human executes the work in Doubao or Cursor. After confirmation, the protocol automatically writes the task back to `SUBMITTED` and then `REVIEW`. A7 remains the quality gate.

See [PHASE_1_5.md](PHASE_1_5.md) for the workflow and how it differs from Phase 1.

Start from the `phase1/` directory:

```bash
python3 scripts/assisted_pilot.py \
  --task-id T-ASSIST-001 \
  --role A3-backend-engineer \
  --title "Add safe_div function"
```

## Static board (demo)

The board in `ui/` lists tasks, status, and the latest audit record. You can pick an executor (Doubao, Codex, Claude, or Cursor) and generate a copy-paste prompt that includes the task text plus a ROLE_SPEC summary.

```bash
# from phase1/
python3 scripts/export_for_ui.py          # uses /tmp/phase1_protocol.db if present
python3 scripts/export_for_ui.py --demo   # rewrite the built-in demo snapshot
python3 scripts/ui_server.py              # http://127.0.0.1:8765/  enables "mark submitted"
python3 scripts/mark_submitted.py --task-id T-DEMO-001 --actor doubao
```

Opening `ui/index.html` as a local file is enough to copy prompts. Changing status requires the local server or `mark_submitted.py`.
