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

A read-only board lives in `ui/`. It lists tasks, status, and recent audit records from a JSON snapshot. It does not talk to a live backend.

```bash
# from phase1/
python3 scripts/export_for_ui.py          # uses /tmp/phase1_protocol.db if present
python3 scripts/export_for_ui.py --demo   # rewrite the built-in demo snapshot
```

Then open `ui/index.html` in a browser (the page reads `ui/data.js`, so a local file open is enough).
