#!/usr/bin/env python3
"""Tests for the per-owner worklist reader (read-only view over cards + pool)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import owner_worklist


def _console_db(path: Path, rows) -> Path:
    """rows: list of (task_id, status, claimed_by)."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT, "
                "claimed_by TEXT)")
    con.executemany("INSERT INTO tasks VALUES (?,?,?)", rows)
    con.commit()
    con.close()
    return path


def _bridge_root(tmp_path: Path, cards, *, signal=None) -> Path:
    root = tmp_path / "bridge"
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "signals").mkdir(parents=True, exist_ok=True)
    for card in cards:
        (root / "tasks" / f"{card['task_id']}.json").write_text(
            json.dumps(card, ensure_ascii=False), encoding="utf-8")
    if signal is not None:
        (root / "signals" / f"{signal['owner']}.json").write_text(
            json.dumps(signal, ensure_ascii=False), encoding="utf-8")
    return root


def _card(task_id, owner, status):
    return {"task_id": task_id,
            "execution": {"owner": owner, "status": status, "attempt": 0},
            "audit": {"verdict": None}}


def test_owner_mismatch_is_reported_when_pool_still_holds_old_owner(tmp_path):
    """The card moved to another agent but the console still claims it for the old one."""
    root = _bridge_root(tmp_path, [_card("T-1", "new-owner", "dispatched")])
    db = _console_db(tmp_path / "pool.db",
                     [("T-1", "CLAIMED", "old-owner")])
    result = owner_worklist.collect("new-owner", root, db)
    anomalies = [a for a in result["anomalies"] if a.get("kind") == "owner_mismatch"]
    assert len(anomalies) == 1
    assert anomalies[0]["tid"] == "T-1"
    assert anomalies[0]["console_owner"] == "old-owner"


def test_unclaimed_pool_row_is_not_an_owner_mismatch(tmp_path):
    """A dispatched card the console has not handed to anybody yet is normal."""
    root = _bridge_root(tmp_path, [_card("T-1", "new-owner", "dispatched")])
    db = _console_db(tmp_path / "pool.db", [("T-1", "READY", None)])
    result = owner_worklist.collect("new-owner", root, db)
    anomalies = [a for a in result["anomalies"] if a.get("kind") == "owner_mismatch"]
    assert anomalies == []


def test_card_missing_from_the_console_pool_is_flagged(tmp_path):
    root = _bridge_root(tmp_path, [_card("T-1", "new-owner", "dispatched")])
    db = _console_db(tmp_path / "pool.db", [("T-OTHER", "READY", None)])
    result = owner_worklist.collect("new-owner", root, db)
    kinds = {a.get("kind") for a in result["anomalies"]}
    assert "not_in_console" in kinds


def test_matching_owner_produces_no_owner_anomaly(tmp_path):
    root = _bridge_root(tmp_path, [_card("T-1", "new-owner", "claimed")])
    db = _console_db(tmp_path / "pool.db", [("T-1", "CLAIMED", "new-owner")])
    result = owner_worklist.collect("new-owner", root, db)
    anomalies = [a for a in result["anomalies"] if a.get("kind") in
                 ("owner_mismatch", "not_in_console")]
    assert anomalies == []
