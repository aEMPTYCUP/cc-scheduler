#!/usr/bin/env python3
"""Hermetic tests for the N-20 resident projector CLI."""
from __future__ import annotations

import pytest

import sync_watch


def test_once_runs_a_single_pass(tmp_path, monkeypatch):
    calls = []

    def fake_sync_once(root):
        calls.append(str(root))
        return {"ok": True, "last_seq": 1, "applied": []}

    monkeypatch.setattr(sync_watch.bridge_sync, "sync_once", fake_sync_once)
    rc = sync_watch.main(["--bridge-root", str(tmp_path), "--once"])
    assert rc == 0
    assert len(calls) == 1


def test_watch_loops_until_keyboard_interrupt(tmp_path, monkeypatch):
    calls = []

    def fake_sync_once(root):
        calls.append(str(root))
        return {"ok": True, "last_seq": 1, "applied": []}

    monkeypatch.setattr(sync_watch.bridge_sync, "sync_once", fake_sync_once)
    monkeypatch.setattr(sync_watch.time, "sleep",
                        lambda _s: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        sync_watch.main(["--bridge-root", str(tmp_path), "--interval", "5"])
    assert len(calls) == 1


def test_sync_error_keeps_watcher_alive(tmp_path, monkeypatch):
    calls = []

    def failing_then_ok(root):
        calls.append(str(root))
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"ok": True, "last_seq": 2, "applied": []}

    monkeypatch.setattr(sync_watch.bridge_sync, "sync_once", failing_then_ok)
    sleeps = []
    def fake_sleep(_s):
        sleeps.append(_s)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt()
    monkeypatch.setattr(sync_watch.time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        sync_watch.main(["--bridge-root", str(tmp_path), "--interval", "5"])
    # First pass failed, second succeeded before the interrupt surfaced.
    assert len(calls) == 2
