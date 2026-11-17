#!/usr/bin/env python3
r"""Tests for atomic_claim (race detection + implementer identity) and
register_role (upsert that never deletes the referenced parent row).

Task T-06. Every test uses the conftest "db_path" fixture, so the protocol
DB and the generated task packages are redirected into pytest's own tmp_path
-- nothing touches the real /tmp, C:\tmp or the user home directory.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

import protocol

ROLE_ID = "A3-backend-engineer"
TASK_ID = "T-06-claim-and-role"

# Role file bodies used by the upsert test; the hash of the file is what
# register_role stores in role_specs.content_hash.
ROLE_CONTENT_V1 = "# role spec v1\n"
ROLE_CONTENT_V2 = "# role spec v2\n"


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the test database with the foreign keys the protocol uses."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _prepare_ready_task(db_path: Path, task_id: str = TASK_ID) -> str:
    """Create a task that sits in READY, waiting to be claimed."""
    assert protocol.DB_PATH == db_path
    protocol.init_db()
    protocol.register_role(ROLE_ID)
    protocol.create_task(
        task_id,
        title="Claim and role test task",
        description="Exercise atomic_claim and register_role.",
        role_id=ROLE_ID,
        acceptance_criteria="The task is claimed exactly once.",
    )
    protocol.ready_task(task_id)
    return task_id


def _role_row(db_path: Path) -> sqlite3.Row:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT rowid, content_hash, content FROM role_specs WHERE role_id=?",
            (ROLE_ID,),
        ).fetchone()
    assert row is not None
    return row


def test_concurrent_claim_has_exactly_one_winner(db_path: Path) -> None:
    """Two threads racing for the same READY task: one wins, one fails."""
    _prepare_ready_task(db_path)

    barrier = threading.Barrier(2, timeout=30)
    results: list[tuple[str, object]] = []
    lock = threading.Lock()

    def worker(agent_id: str) -> None:
        try:
            barrier.wait()
            package = protocol.atomic_claim(TASK_ID, agent_id=agent_id)
        except Exception as exc:  # the loser may raise ValueError or RuntimeError
            with lock:
                results.append(("error", exc))
        else:
            with lock:
                results.append(("ok", package))

    threads = [
        threading.Thread(target=worker, args=("agent-alpha",)),
        threading.Thread(target=worker, args=("agent-beta",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert len(results) == 2

    winners = [payload for kind, payload in results if kind == "ok"]
    losers = [payload for kind, payload in results if kind == "error"]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], (ValueError, RuntimeError))

    task = protocol.get_task(TASK_ID)
    assert task["status"] == "CLAIMED"
    assert task["claimed_by"] in ("agent-alpha", "agent-beta")
    assert task["implementer_executor_id"] is None

    with closing(_connect(db_path)) as conn:
        claims = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE task_id=?", (TASK_ID,)
        ).fetchone()[0]
    assert claims == 1


def test_second_claim_of_the_same_task_raises(db_path: Path) -> None:
    """Once a task is CLAIMED it can no longer be claimed again."""
    _prepare_ready_task(db_path)
    protocol.atomic_claim(TASK_ID, agent_id="agent-alpha")

    with pytest.raises(ValueError):
        protocol.atomic_claim(TASK_ID, agent_id="agent-beta")

    task = protocol.get_task(TASK_ID)
    assert task["status"] == "CLAIMED"
    assert task["claimed_by"] == "agent-alpha"


def test_register_role_upserts_without_dropping_the_referenced_row(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-registering a role updates it in place; referencing tasks survive."""
    roles_dir = tmp_path / "roles"
    agent_md = roles_dir / ROLE_ID / "AGENT.md"
    agent_md.parent.mkdir(parents=True)
    agent_md.write_text(ROLE_CONTENT_V1, encoding="utf-8")
    monkeypatch.setattr(protocol, "ROLES_DIR", roles_dir)

    protocol.init_db()
    protocol.register_role(ROLE_ID)
    before = _role_row(db_path)
    assert before["content_hash"] == hashlib.sha256(
        ROLE_CONTENT_V1.encode()
    ).hexdigest()

    protocol.create_task(
        TASK_ID,
        title="Upsert test task",
        description="A task that references the role being re-registered.",
        role_id=ROLE_ID,
        acceptance_criteria="The task keeps its role reference.",
    )
    protocol.ready_task(TASK_ID)

    # The role file changes -> the second registration must update in place.
    agent_md.write_text(ROLE_CONTENT_V2, encoding="utf-8")
    protocol.register_role(ROLE_ID)

    after = _role_row(db_path)
    assert after["content_hash"] == hashlib.sha256(
        ROLE_CONTENT_V2.encode()
    ).hexdigest()
    assert after["content_hash"] != before["content_hash"]
    assert after["content"] == ROLE_CONTENT_V2
    # Updated in place: the row was not deleted and re-inserted, so the
    # tasks -> role_specs foreign key never sees a missing parent.
    assert after["rowid"] == before["rowid"]

    task = protocol.get_task(TASK_ID)
    assert task is not None
    assert task["role_id"] == ROLE_ID
    assert task["status"] == "READY"

    with closing(_connect(db_path)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_id=?", (TASK_ID,)
        ).fetchone()[0] == 1


def test_claim_freezes_the_implementer_identity(db_path: Path) -> None:
    """A known active executor is written to tasks.implementer_executor_id."""
    _prepare_ready_task(db_path)

    protocol.atomic_claim(TASK_ID, agent_id=ROLE_ID, executor_id="codex")

    task = protocol.get_task(TASK_ID)
    assert task["status"] == "CLAIMED"
    assert task["claimed_by"] == ROLE_ID
    assert task["implementer_executor_id"] == "codex"


def test_claim_with_unknown_executor_id_raises(db_path: Path) -> None:
    """An executor that is not in the registry is rejected."""
    _prepare_ready_task(db_path)

    with pytest.raises(ValueError):
        protocol.atomic_claim(TASK_ID, agent_id=ROLE_ID, executor_id="not-an-executor")

    task = protocol.get_task(TASK_ID)
    assert task["status"] == "READY"
    assert task["claimed_by"] is None
    assert task["implementer_executor_id"] is None


def test_claim_with_inactive_executor_id_raises(db_path: Path) -> None:
    """A deactivated executor is rejected."""
    _prepare_ready_task(db_path)
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE executors SET active=0 WHERE executor_id='cursor'")
        conn.commit()

    with pytest.raises(ValueError):
        protocol.atomic_claim(TASK_ID, agent_id=ROLE_ID, executor_id="cursor")

    task = protocol.get_task(TASK_ID)
    assert task["status"] == "READY"
    assert task["implementer_executor_id"] is None


def test_legacy_claim_without_executor_id_keeps_identity_null(db_path: Path) -> None:
    """The old two-argument call still works and leaves the identity NULL."""
    _prepare_ready_task(db_path)

    protocol.atomic_claim(TASK_ID, ROLE_ID)

    task = protocol.get_task(TASK_ID)
    assert task["status"] == "CLAIMED"
    assert task["claimed_by"] == ROLE_ID
    assert task["implementer_executor_id"] is None


def test_claim_still_writes_the_task_package(db_path: Path, tmp_path: Path) -> None:
    """Regression: the package, its hash and the manifest are still written."""
    _prepare_ready_task(db_path)

    package = protocol.atomic_claim(TASK_ID, agent_id=ROLE_ID)

    pkg_dir = tmp_path / "task-packages" / TASK_ID
    manifest = json.loads((pkg_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["package_hash"] == package["package_hash"]
    assert manifest["task_id"] == TASK_ID
    assert manifest["role_id"] == ROLE_ID
    assert manifest["claimed_by"] == ROLE_ID
    assert manifest["role_spec_sha256"] == package["manifest"]["role_spec_sha256"]
    # The implementer identity lives in the database column, not in the
    # package hash -- the hash logic and the ROLE_SPEC binding are untouched.
    assert "executor_id" not in manifest
    assert "implementer_executor_id" not in manifest
    assert (pkg_dir / "ROLE_SPEC.md").read_text(encoding="utf-8") == package["ROLE_SPEC.md"]

    with closing(_connect(db_path)) as conn:
        stored = conn.execute(
            "SELECT content FROM role_specs WHERE role_id=?", (ROLE_ID,)
        ).fetchone()[0]
    assert package["ROLE_SPEC.md"] == stored

    task = protocol.get_task(TASK_ID)
    assert task["package_hash"] == package["package_hash"]
    assert task["claimed_by"] == ROLE_ID


def test_claim_race_check_uses_the_statement_rowcount() -> None:
    """The race check must read the UPDATE cursor, not the connection total.

    conn.total_changes is a running total of every change made on the
    connection, so it only happens to be zero when the claim UPDATE is the
    first write statement. cur.rowcount is the number of rows the UPDATE
    actually touched.
    """
    tree = ast.parse(inspect.getsource(protocol.atomic_claim))
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "total_changes" not in attributes
    assert "rowcount" in attributes
