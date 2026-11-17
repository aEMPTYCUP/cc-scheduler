#!/usr/bin/env python3
r"""Tests for the v3 integration node and the real git-backed A8 adapter.

Git cases build throwaway repositories in pytest's tmp_path: no real project,
no push, no formal queue. The protocol DB still uses the db_path fixture, so
nothing escapes the sandbox.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import protocol
import orchestrator

ROLE = "A3-backend-engineer"
IMPLEMENTER = "codex"
REVIEWER = "claude"
INTEGRATOR = "claude"


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _make_repo(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "test")
    (path / "f.txt").write_text("base line\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")


def _make_role() -> None:
    orchestrator.ensure_orchestrator()
    with protocol.get_db() as conn:
        conn.execute(
            "INSERT INTO role_specs (role_id, name, content_hash, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ROLE, ROLE, "hash", "# role spec", protocol.utcnow()),
        )
        conn.commit()


def _task_to_accepted(task_id: str) -> None:
    protocol.create_task(task_id, task_id, "desc", ROLE, "criteria")
    protocol.ready_task(task_id, actor="seed")
    protocol.atomic_claim(task_id, agent_id=IMPLEMENTER, executor_id=IMPLEMENTER)
    protocol.transition(task_id, "IN_PROGRESS", IMPLEMENTER, actor_executor_id=IMPLEMENTER)
    protocol.transition(task_id, "SUBMITTED", IMPLEMENTER, actor_executor_id=IMPLEMENTER)
    protocol.transition(task_id, "REVIEW", IMPLEMENTER, actor_executor_id=IMPLEMENTER)
    # Independent reviewer (different identity) accepts.
    protocol.transition(task_id, "ACCEPTED", REVIEWER, actor_executor_id=REVIEWER)


# --------------------------------------------------------------------------- #
# capability policy
# --------------------------------------------------------------------------- #

def test_integration_capability_policy(db_path) -> None:
    orchestrator.ensure_orchestrator()
    caps = {(c["executor_id"], c["node"]): c for c in orchestrator.list_capabilities()}
    assert caps[("claude", "integrate")]["enabled"] == 1       # A8 owner, on by default
    assert caps[("doubao", "integrate")]["locked"] == 1        # coding-only, locked
    with pytest.raises(orchestrator.CapabilityLocked):
        orchestrator.set_capability("doubao", "integrate", True)


def test_simulated_integration_drains_accepted_to_done(db_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    _task_to_accepted("T1")
    orchestrator.start_worker("claude", "integrate")
    results = orchestrator.run_worker_drain("claude", "integrate")
    assert results[0]["status"] == "worked"
    assert protocol.get_task("T1")["status"] == "DONE"


def test_integration_selection_only_offers_accepted(db_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    protocol.create_task("TR", "t", "d", ROLE, "c")
    protocol.ready_task("TR", actor="seed")  # READY, not ACCEPTED
    assert orchestrator.select_next_task_for_node("integrate", "claude") is None
    _task_to_accepted("T1")
    chosen = orchestrator.select_next_task_for_node("integrate", "claude")
    assert chosen and chosen["task_id"] == "T1"


# --------------------------------------------------------------------------- #
# real git integration
# --------------------------------------------------------------------------- #

def test_git_adapter_clean_merge_reaches_done(db_path, tmp_path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo)
    _make_role()
    orchestrator.ensure_orchestrator()

    task_id = "T-CLEAN"
    _task_to_accepted(task_id)
    wt = protocol.create_worktree(str(repo), task_id)
    (Path(wt) / "new.txt").write_text("feature\n", encoding="utf-8")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "feature on task branch")

    task = protocol.get_task(task_id)
    summary = orchestrator.GitIntegrationAdapter(str(repo), "main").integrate(task, INTEGRATOR)
    assert "-> DONE" in summary
    assert protocol.get_task(task_id)["status"] == "DONE"

    log = _git(repo, "log", "--oneline", "main").stdout
    assert f"Merge task {task_id}" in log
    assert (repo / "new.txt").read_text(encoding="utf-8") == "feature\n"
    # Merge commit recorded as evidence.
    with protocol.get_db() as conn:
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM task_evidence WHERE task_id=?", (task_id,))]
    assert "commit_sha" in kinds


def test_git_adapter_conflict_returns_to_review_and_keeps_main_clean(db_path, tmp_path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo)
    _make_role()
    orchestrator.ensure_orchestrator()

    task_id = "T-CONFLICT"
    _task_to_accepted(task_id)
    wt = protocol.create_worktree(str(repo), task_id)
    (Path(wt) / "f.txt").write_text("branch edit\n", encoding="utf-8")
    _git(wt, "add", "."); _git(wt, "commit", "-m", "branch edit")

    # A competing change on main touches the same line -> real conflict.
    (repo / "f.txt").write_text("main edit\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "main edit")
    main_head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    task = protocol.get_task(task_id)
    summary = orchestrator.GitIntegrationAdapter(str(repo), "main").integrate(task, INTEGRATOR)
    assert "sent back to REVIEW" in summary
    assert protocol.get_task(task_id)["status"] == "REVIEW"
    # Merge aborted: clean tree and main untouched.
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == main_head_before


def test_git_adapter_failing_post_merge_check_returns_to_review(db_path, tmp_path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo)
    _make_role()
    orchestrator.ensure_orchestrator()

    task_id = "T-CHECK"
    _task_to_accepted(task_id)
    wt = protocol.create_worktree(str(repo), task_id)
    (Path(wt) / "ok.txt").write_text("x\n", encoding="utf-8")
    _git(wt, "add", "."); _git(wt, "commit", "-m", "ok")
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    task = protocol.get_task(task_id)
    adapter = orchestrator.GitIntegrationAdapter(str(repo), "main", check_command="exit 1")
    summary = adapter.integrate(task, INTEGRATOR)
    assert "failed checks" in summary
    assert protocol.get_task(task_id)["status"] == "REVIEW"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_git_adapter_requires_a_real_worktree(db_path, tmp_path) -> None:
    _make_role()
    orchestrator.ensure_orchestrator()
    _task_to_accepted("T-NOWT")
    task = protocol.get_task("T-NOWT")
    assert task["worktree_path"] is None
    with pytest.raises(ValueError, match="no worktree_path"):
        orchestrator.GitIntegrationAdapter(str(tmp_path), "main").integrate(task, INTEGRATOR)


# --------------------------------------------------------------------------- #
# v2 -> v3 migration widens the node CHECK without losing rows
# --------------------------------------------------------------------------- #

def test_v2_to_v3_migration_widens_node_check(db_path) -> None:
    import sqlite3
    orchestrator.ensure_orchestrator()
    with sqlite3.connect(db_path) as conn:
        # Rebuild the table the v2 way with the two-node CHECK and one row.
        conn.executescript("""
        DROP TABLE executor_capabilities;
        CREATE TABLE executor_capabilities (
            executor_id TEXT NOT NULL,
            node TEXT NOT NULL CHECK (node IN ('implement','acceptance')),
            enabled INTEGER NOT NULL DEFAULT 0,
            locked INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (executor_id, node)
        );
        INSERT INTO executor_capabilities VALUES ('claude','implement',1,0,'2026-01-01');
        PRAGMA user_version=2;
        """)
        conn.commit()
    orchestrator.ensure_orchestrator()  # triggers v2 -> v3 rebuild + reseed
    with sqlite3.connect(db_path) as conn:
        # Old row preserved.
        row = conn.execute(
            "SELECT enabled FROM executor_capabilities WHERE executor_id='claude' AND node='implement'"
        ).fetchone()
        assert row and row[0] == 1
        # The widened CHECK now admits an 'integrate' row (seeded during migration).
        integrated = conn.execute(
            "SELECT COUNT(*) FROM executor_capabilities WHERE node='integrate'"
        ).fetchone()[0]
        assert integrated >= 1
        # The CHECK is still enforced: an unknown node is rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO executor_capabilities VALUES ('human','teleport',1,0,'2026-01-03')")
        assert protocol.SCHEMA_VERSION == 3
