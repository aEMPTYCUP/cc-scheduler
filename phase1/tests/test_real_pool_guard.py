#!/usr/bin/env python3
"""Guard tests: a simulated adapter must never drive a production task pool.

The pool mode is stored in-pool (pool_meta); a real imported pool is marked
'production' by the importer, while any unmarked DB stays 'demo' so existing
simulated demos and the rest of the suite keep working.
"""
from __future__ import annotations

import pytest

import orchestrator
import protocol


def test_schema_is_v4_with_pool_meta(db_path):
    protocol.ensure_schema()
    with protocol.get_db() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert protocol.SCHEMA_VERSION == 4
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pool_meta'"
            )
        ]
        assert names == ["pool_meta"]


def test_pool_mode_defaults_to_demo_and_persists(db_path):
    protocol.ensure_schema()
    assert protocol.get_pool_mode() == "demo"  # fail-safe default
    protocol.set_pool_mode("production")
    assert protocol.get_pool_mode() == "production"
    with pytest.raises(ValueError):
        protocol.set_pool_mode("nope")


def test_adapter_class_flags():
    assert orchestrator.WorkerAdapter.is_simulated is False
    assert orchestrator.SimulatedAdapter.is_simulated is True
    assert orchestrator.GitIntegrationAdapter.is_simulated is False


def test_demo_pool_allows_simulated_go(db_path, monkeypatch):
    orchestrator.ensure_orchestrator()
    assert protocol.get_pool_mode() == "demo"
    monkeypatch.setattr(orchestrator, "_active_adapter_simulated", True)
    result = orchestrator.start_worker("doubao", "implement")
    try:
        assert result["running"] is True
    finally:
        orchestrator.stop_worker("doubao", "implement")


def test_production_pool_blocks_simulated_go(db_path, monkeypatch):
    orchestrator.ensure_orchestrator()
    protocol.set_pool_mode("production")
    monkeypatch.setattr(orchestrator, "_active_adapter_simulated", True)
    with pytest.raises(orchestrator.RealPoolSimulationGuard):
        orchestrator.start_worker("doubao", "implement")
    # The session must not have been started and no task state changed.
    with protocol.get_db() as conn:
        running = conn.execute(
            "SELECT running FROM worker_sessions "
            "WHERE executor_id='doubao' AND node='implement'"
        ).fetchone()["running"]
    assert running == 0


def test_production_pool_allows_go_with_real_adapter(db_path, monkeypatch):
    orchestrator.ensure_orchestrator()
    protocol.set_pool_mode("production")
    monkeypatch.setattr(orchestrator, "_active_adapter_simulated", False)
    result = orchestrator.start_worker("doubao", "implement")
    try:
        assert result["running"] is True
    finally:
        orchestrator.stop_worker("doubao", "implement")


def test_console_state_exposes_guard_fields(db_path, monkeypatch):
    orchestrator.ensure_orchestrator()
    protocol.set_pool_mode("production")
    monkeypatch.setattr(orchestrator, "_active_adapter_simulated", True)
    state = orchestrator.console_state()
    assert state["pool_mode"] == "production"
    assert state["adapter_simulated"] is True
    assert state["sim_go_blocked"] is True
