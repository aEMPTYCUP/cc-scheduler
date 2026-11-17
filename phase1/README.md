# Phase 1 Protocol Layer

Thin Git-backed task pool + SQLite state machine for the multi-agent development factory.

The protocol layer only dispatches work, records state and writes the audit trail; the actual execution still happens in Doubao / Codex / Claude / Cursor, or by hand.

Related documents:

- [P1_SCHEMA_DESIGN.md](P1_SCHEMA_DESIGN.md) - P1 schema v1, state machine, self-acceptance guard, test plan
- [PHASE_2_REPLACEMENT.md](PHASE_2_REPLACEMENT.md) - the P2 / P3 / P4 roadmap
- [PHASE_1_5.md](PHASE_1_5.md) - assisted (copy-paste) workflow
- [PROJECT_STATUS_AND_NEXT_STEPS.md](PROJECT_STATUS_AND_NEXT_STEPS.md) - project status and the three work lines
- [LANDING_CHECKLIST.md](LANDING_CHECKLIST.md) - landing checklist

## 1. Requirements and installation

- Python 3.11 (CI uses 3.11 as well)
- The protocol layer itself uses only the standard library
- Test dependencies live in `phase1/requirements-dev.txt` (`pytest>=8`)

This repository already carries a virtual environment at `.venv/`:

```powershell
# from the repository root
./.venv/Scripts/python.exe -m pip install -r phase1/requirements-dev.txt
```

## 2. Where the database lives (PHASE1_DB)

The database path is resolved in exactly one place, `protocol._resolve_db_path()`, with this priority:

1. **The `PHASE1_DB` environment variable** - highest priority, this is the one to set
2. **The legacy `/tmp/phase1_protocol.db`** - used only when that file already exists (on Windows `/tmp` resolves to `C:/tmp`)
3. **The system temp directory** - `<system temp dir>/phase1/phase1_protocol.db`, i.e. `%TEMP%/phase1/phase1_protocol.db` on Windows

The path is **no longer hardcoded to `/tmp`**. `protocol.get_db()` creates the parent directory for you, so `PHASE1_DB` may point at a path that does not exist yet.

```powershell
# Keep the database inside the repository (recommended for local debugging)
$env:PHASE1_DB = "$PWD/phase1/.local/protocol.db"
```

`phase1/scripts/export_for_ui.py` imports the same value (`export_for_ui.DEFAULT_DB is protocol.DB_PATH`), so one environment variable switches the protocol layer and the dashboard over together.

## 3. Initialize the database and the roles

```powershell
cd phase1
../.venv/Scripts/python.exe cli.py init
```

`cli.py init` does two things: `protocol.init_db()` (creates schema v1 and seeds the five controlled executors `doubao` / `codex` / `claude` / `cursor` / `human`) and `protocol.register_role()` for the four roles A0 / A3 / A7 / A8 - the content of each role file (for example `phase1/roles/A3-backend-engineer/AGENT.md`) is hashed with SHA256 and stored in `role_specs`.

A legacy v0 database is migrated to v1 automatically on the first `init`; see [P1_SCHEMA_DESIGN.md](P1_SCHEMA_DESIGN.md) section 4.

## 4. Creating tasks

Via the CLI (create + READY in one step):

```powershell
../.venv/Scripts/python.exe cli.py create T-001 --title "Add safe_div function" --description "..." --role A3-backend-engineer --acceptance "Unit test passes"
```

Via the Python API, including the P1 structured contract:

```python
import sys
sys.path.insert(0, "phase1")   # run from the repository root
import protocol

protocol.create_task(
    "T-002",
    title="Implement X",
    description="...",
    role_id="A3-backend-engineer",
    acceptance_criteria="...",
    created_by="A0",
    priority="high",                        # high / medium / low, default medium
    dependencies=["T-001"],                 # must already exist
    allowed_paths=["phase1/protocol.py"],  # JSON array
    behavioral_check="./.venv/Scripts/python.exe -m pytest phase1/tests -q",
    pass_condition="86 passed",
    max_retries=3,
)
protocol.ready_task("T-002")
```

Runtime validation:

- `priority` must be one of `high` / `medium` / `low`
- `max_retries` must be a non-negative integer
- **The tasks listed in `dependencies` must already exist.** Forward references and self-references are not supported: an unknown dependency raises `ValueError`. Create the upstream task first, then the downstream task that depends on it.
- The dependency relation is written to `task_dependencies` (the authoritative source); `tasks.dependencies_json` holds the deduplicated, sorted snapshot the dashboard reads

## 5. Claiming and advancing the state

```python
protocol.atomic_claim("T-002", agent_id="A3-backend-engineer", executor_id="codex")
protocol.transition("T-002", "IN_PROGRESS", actor="A3-backend-engineer", reason="start work")
protocol.transition("T-002", "SUBMITTED", actor="A3-backend-engineer",
                    evidence="86 passed",
                    evidence_path="phase1/artifacts/T-002/run.log")
```

`executor_id` is optional. When it is given it must name a row in `executors` with `active = 1`, and it is frozen into `tasks.implementer_executor_id` as the implementer identity - that identity is what the self-acceptance guard compares against.

`protocol.select_next_task()` picks the next piece of work **read-only**: it returns the READY task whose dependencies are all DONE, ordered by priority, then `created_at`, then `task_id`. It changes no status and writes no audit row; claiming stays the job of `atomic_claim()`.

## 6. Running the tests

```powershell
# from the repository root
./.venv/Scripts/python.exe -m pytest phase1/tests -q
```

Current state: **86 tests, all green** (measured output `86 passed in 9.17s`; the runtime drifts with the machine). The 8 test files:

| File | Tests |
| --- | --- |
| `phase1/tests/test_smoke.py` | 1 |
| `phase1/tests/test_db_path.py` | 6 |
| `phase1/tests/test_claim_and_role.py` | 9 |
| `phase1/tests/test_state_machine.py` | 21 |
| `phase1/tests/test_selection.py` | 21 |
| `phase1/tests/test_failure_and_evidence.py` | 14 |
| `phase1/tests/test_independence_guard.py` | 7 |
| `phase1/tests/test_schema_migration.py` | 7 |

`phase1/tests/conftest.py` points pytest `basetemp` at a private directory under the system temp dir (`%TEMP%/ccs-pytest-basetemp`). That avoids the `pytest-of-<user>` folder left behind by another account on this machine, which cannot be listed and used to make pytest die with a PermissionError even though every assertion passed. Every database the tests touch lives in pytest's own `tmp_path`, so no test writes to the real `/tmp`, `C:/tmp` or the user home directory.

CI: `.github/workflows/protocol-ci.yml` (workflow name "Multi-Agent Protocol CI"), `permissions: contents: read`. It installs `phase1/requirements-dev.txt` and runs `python -m pytest phase1/tests -q`. There is no `|| true` swallowing failures - a failing test turns the workflow red.

## 7. Board export and local service

```powershell
cd phase1
../.venv/Scripts/python.exe scripts/export_for_ui.py             # export from protocol.DB_PATH
../.venv/Scripts/python.exe scripts/export_for_ui.py --demo      # write the built-in demo snapshot
../.venv/Scripts/python.exe scripts/export_for_ui.py --db <db> --out <json>
../.venv/Scripts/python.exe scripts/ui_server.py                 # http://127.0.0.1:8765/
../.venv/Scripts/python.exe scripts/mark_submitted.py --task-id T-001 --actor doubao
```

- `export_for_ui.py` writes `phase1/ui/data.json` plus the `phase1/ui/data.js` wrapper; when the database does not exist it writes the built-in demo snapshot instead.
- `ui_server.py` serves `phase1/ui/` at http://127.0.0.1:8765/ and backs the "mark as submitted" button. Demo data is never modified.
- `mark_submitted.py` pushes a task to SUBMITTED (a CLAIMED task first goes to IN_PROGRESS). It does not advance to REVIEW on its own.

Opening `phase1/ui/index.html` as a local file is enough to copy prompts. Changing status requires the local service or `mark_submitted.py`.

## 8. Phase 1.5 assisted path (still available)

```powershell
cd phase1
../.venv/Scripts/python.exe scripts/assisted_pilot.py --task-id T-ASSIST-001 --role A3-backend-engineer --title "Add safe_div function"
```

The system creates the task package and a copy-paste prompt that includes the bound ROLE_SPEC. A human executes the work in Doubao or Cursor. After confirmation, the protocol automatically writes the task back to SUBMITTED and then REVIEW. A7 remains the quality gate. See [PHASE_1_5.md](PHASE_1_5.md) for the workflow and how it differs from Phase 1.

## 9. What P1 added to the protocol layer (2026-11-17)

P1 landed as tasks T-01 ~ T-09; the schema, the state machine and the guard are detailed in [P1_SCHEMA_DESIGN.md](P1_SCHEMA_DESIGN.md). Summary:

New functions:

| Function | Purpose |
| --- | --- |
| `protocol.select_next_task()` | Read-only selection: the READY task whose dependencies are all DONE, ordered by priority, then `created_at`, then `task_id` |
| `protocol.record_failure(task_id, actor, error, reason="")` | Counts one failed attempt: `retry_count` + 1 and `last_error` are written; the task lands in FAILED while attempts remain and in BLOCKED once `max_retries` is reached (default 3) |
| `protocol.block_task(task_id, actor, reason="")` | Moves a task to BLOCKED |
| `protocol.request_human(task_id, actor, reason="")` | Moves a task to NEEDS_HUMAN |
| `protocol.release_claim(task_id, actor, reason="claim released")` | Stamps `claims.released_at` and writes a self-loop audit row only. The task status is left untouched on purpose - the state machine has no CLAIMED -> READY edge |
| `protocol.add_evidence(task_id, kind, path, created_by, sha256=None, sync_audit=True)` | Stores one row in `task_evidence`; `kind` must be one of `cli_log` / `diff_patch` / `commit_sha` / `package_hash` / `other`. With `sync_audit` the newest audit row points at the same path through `audit_log.evidence_path` |

New keyword arguments on existing functions (all optional, old calls keep working):

- `create_task(..., priority="medium", dependencies=None, allowed_paths=None, behavioral_check="", pass_condition="", max_retries=3)`
- `atomic_claim(task_id, agent_id, executor_id=None)`
- `transition(..., evidence_path=None, actor_executor_id=None)`

Three new exception states:

- **FAILED** - one attempt failed and may be retried; `FAILED -> IN_PROGRESS` and `FAILED -> BLOCKED`
- **NEEDS_HUMAN** - a human has to take over; `NEEDS_HUMAN -> IN_PROGRESS`, `-> REVIEW` and `-> BLOCKED`
- **BLOCKED** - retries exhausted or not solvable automatically; `BLOCKED -> IN_PROGRESS` and `-> NEEDS_HUMAN`

The original ten states and their edges are unchanged.
