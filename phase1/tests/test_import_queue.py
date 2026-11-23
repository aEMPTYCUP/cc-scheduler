#!/usr/bin/env python3
"""Tests for the formal-queue -> Phase 1 console importer.

Hermetic: a tiny synthetic queue, a stub zero-LLM matcher, and a tmp DB via the
shared ``db_path`` fixture. The real futures queue.json and agent-pool are never
read, and no role files are written into the repository.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PHASE1_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = PHASE1_DIR / "scripts"
for p in (str(PHASE1_DIR), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import protocol  # noqa: E402
import import_queue as imp  # noqa: E402


class _FakeMatcher:
    """Returns a preset slug per task id; None models a matcher miss."""

    def __init__(self, slug_by_id):
        self._slug = slug_by_id

    def match_many(self, tasks):
        return [
            {"slug": self._slug.get(t["id"], "engineering-backend-architect"),
             "tier": 1, "reason": "stub", "candidates": []}
            for t in tasks
        ]


def _write_queue(tmp_path: Path, tasks) -> Path:
    q = tmp_path / "queue.json"
    q.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8")
    return q


def _task(tid, status, *, deps=None, priority="medium", title=None):
    return {
        "id": tid, "title": title or f"任务 {tid}", "description": "d",
        "priority": priority, "status": status,
        "allowed_paths": ["backend/app/x.py"], "behavioral_check": "pytest",
        "pass_condition": "绿", "dependencies": deps or [], "redo_count": 0,
        "created_at": "2026-10-13T01:02:03Z",
    }


@pytest.fixture
def isolated_importer(tmp_path, db_path, monkeypatch):
    # db_path (a fixture dependency) redirects protocol.DB_PATH before we touch it.
    monkeypatch.setattr(imp, "_load_matcher", lambda pool: _FakeMatcher({
        "T-READY": "engineering-backend-architect",
        "T-BACKLOG": "frontend-developer",
        "T-DONE": "api-test-engineer",
        "T-MISS": None,
    }))
    monkeypatch.setattr(imp, "_provision_roles", lambda pool, slugs: None)
    # The provisioning step is stubbed, so satisfy the tasks->role_specs FK with
    # minimal role rows in the tmp DB (role registration itself is covered elsewhere).
    protocol.ensure_schema()
    with protocol.get_db() as conn:
        for slug in ("engineering-backend-architect", "frontend-developer",
                     "api-test-engineer", imp.FALLBACK_ROLE):
            conn.execute(
                "INSERT OR IGNORE INTO role_specs (role_id, name, content_hash, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)", (slug, slug, "x", "stub role", protocol.utcnow()))
        conn.commit()
    return tmp_path


def _run(tmp_path, db_path, reset=True):
    protocol.ensure_schema()
    q = _write_queue(tmp_path, [
        _task("T-DONE", "passed"),
        _task("T-SUPER", "superseded"),
        _task("T-HUMAN", "needs_human"),
        _task("T-BLOCK", "blocked"),
        _task("T-READY", "pending", priority="high"),               # no deps -> READY
        _task("T-BACKLOG", "pending", deps=["T-READY"]),              # dep not DONE -> BACKLOG
        _task("T-MISS", "pending"),                                   # matcher miss -> fallback
    ])
    return imp.import_queue(q, tmp_path / "pool", reset)


def test_status_mapping_roles_and_contract(isolated_importer, tmp_path, db_path):
    report = _run(tmp_path, db_path)
    assert report["inserted"] == 7 and report["skipped_existing"] == 0
    assert report["statuses"] == {
        "DONE": 2, "NEEDS_HUMAN": 1, "BLOCKED": 1, "READY": 2, "BACKLOG": 1,
    }
    assert report["fallback_tasks"] == ["T-MISS"]

    def status_of(tid):
        return protocol.get_task(tid)["status"]

    assert status_of("T-DONE") == "DONE"
    assert status_of("T-SUPER") == "DONE"
    assert status_of("T-HUMAN") == "NEEDS_HUMAN"
    assert status_of("T-BLOCK") == "BLOCKED"
    assert status_of("T-READY") == "READY"
    assert status_of("T-BACKLOG") == "BACKLOG"

    ready = protocol.get_task("T-READY")
    assert ready["role_id"] == "engineering-backend-architect"
    assert ready["executor_id"] is None                 # channel stays unassigned
    assert json.loads(ready["allowed_paths_json"]) == ["backend/app/x.py"]
    assert ready["behavioral_check"] == "pytest"
    # Matcher miss lands on the manual triage role, never a null role.
    assert protocol.get_task("T-MISS")["role_id"] == imp.FALLBACK_ROLE
    # An imported real pool is marked production so simulated GO is guarded.
    assert protocol.get_pool_mode() == "production"


def test_dependency_edges_and_ready_gate(isolated_importer, tmp_path, db_path):
    _run(tmp_path, db_path)
    with protocol.get_db() as conn:
        deps = {r[0] for r in conn.execute(
            "SELECT depends_on FROM task_dependencies WHERE task_id='T-BACKLOG'")}
    assert deps == {"T-READY"}
    # select_next_task only returns a READY task whose deps are all DONE.
    nxt = protocol.select_next_task()
    assert nxt is not None and nxt["task_id"] == "T-READY"


def test_import_is_idempotent_under_reset_and_skips_without(isolated_importer, tmp_path, db_path):
    _run(tmp_path, db_path)
    # Second run without reset: rows already exist, nothing re-inserted.
    again = _run(tmp_path, db_path, reset=False)
    assert again["inserted"] == 0 and again["skipped_existing"] == 7
    # Reset rebuilds to the same deterministic state.
    rebuilt = _run(tmp_path, db_path, reset=True)
    assert rebuilt["inserted"] == 7 and rebuilt["skipped_existing"] == 0
    assert len(protocol.list_tasks()) == 7
