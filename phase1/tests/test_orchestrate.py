#!/usr/bin/env python3
"""Hermetic tests for the multi-executor orchestration module."""
from __future__ import annotations

import pytest

import orchestrator
import orchestrate
import protocol


def _role() -> None:
    orchestrator.ensure_orchestrator()
    with protocol.get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO role_specs (role_id, name, content_hash, content, created_at) "
            "VALUES ('A3-backend-engineer','A3','h','c',?)", (protocol.utcnow(),))
        conn.commit()


def test_create_plan_wires_dependencies_and_assignments(db_path):
    _role()
    plan = orchestrate.create_plan(
        "T-ORC-1", "父任务", "拆成两个子任务",
        [
            {"task_id": "T-ORC-1a", "title": "实现", "executor": "claude",
             "allowed_paths": ["backend/a.py"]},
            {"task_id": "T-ORC-1b", "title": "测试", "executor": "codex",
             "depends_on": ["T-ORC-1a"], "allowed_paths": ["backend/tests/t.py"]},
        ],
    )
    assert plan["subtasks"] == ["T-ORC-1a", "T-ORC-1b"]
    assert protocol.get_task("T-ORC-1a")["status"] == "READY"
    assert protocol.get_task("T-ORC-1a")["executor_id"] == "claude"
    # The dependent subtask waits for its dependency.
    assert protocol.get_task("T-ORC-1b")["status"] == "BACKLOG"
    assert protocol.get_task("T-ORC-1b")["executor_id"] == "codex"


def test_assigned_task_is_invisible_to_other_executors(db_path):
    _role()
    orchestrate.create_plan(
        "T-ORC-2", "父任务", "",
        [{"task_id": "T-ORC-2a", "title": "codex 的活", "executor": "codex"}],
    )
    # claude must not see a codex-assigned task ...
    assert orchestrator.select_next_task_for_node("implement", "claude") is None
    # ... but codex must.
    picked = orchestrator.select_next_task_for_node("implement", "codex")
    assert picked and picked["task_id"] == "T-ORC-2a"


def test_claim_enforces_the_assignment(db_path):
    _role()
    orchestrate.create_plan(
        "T-ORC-3", "父任务", "",
        [{"task_id": "T-ORC-3a", "title": "codex 的活", "executor": "codex"}],
    )
    with pytest.raises(ValueError, match="assigned to executor"):
        protocol.atomic_claim("T-ORC-3a", agent_id="claude", executor_id="claude")
    claimed = protocol.atomic_claim("T-ORC-3a", agent_id="codex", executor_id="codex")
    assert claimed["manifest"]["task_id"] == "T-ORC-3a"


def test_status_aggregates_subtasks(db_path):
    _role()
    orchestrate.create_plan(
        "T-ORC-4", "父任务", "",
        [
            {"task_id": "T-ORC-4a", "title": "a", "executor": "claude"},
            {"task_id": "T-ORC-4b", "title": "b", "executor": "codex"},
        ],
    )
    report = orchestrate.status("T-ORC-4")
    assert report["total"] == 2
    assert report["done"] == 0
    assert report["by_executor"] == {"claude": 1, "codex": 1}
