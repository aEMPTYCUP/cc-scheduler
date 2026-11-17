#!/usr/bin/env python3
r"""Tests for the single-source DB path resolution (protocol.DB_PATH).

Every test works inside a disposable scratch directory — nothing is written to
the real /tmp, C:\tmp or the user home.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PHASE1_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PHASE1_DIR / "scripts"
for _path in (SCRIPTS_DIR, PHASE1_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import export_for_ui  # noqa: E402
import protocol  # noqa: E402


@pytest.fixture
def scratch(request):
    r"""Disposable scratch directory for the tests.

    This is pytest's own tmp_path. When pytest's temp root is not readable
    (e.g. a sandboxed account whose %TEMP%\pytest-of-<user> folder was left
    behind by another account) fall back to a private stdlib temp directory so
    the test still runs isolated from the real /tmp, C:\tmp and the user home.
    """
    try:
        return Path(request.getfixturevalue("tmp_path"))
    except OSError:
        path = Path(tempfile.mkdtemp(prefix="phase1-dbpath-"))
        request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
        return path


def test_env_var_takes_priority(monkeypatch, scratch):
    monkeypatch.setenv("PHASE1_DB", str(scratch / "pool.db"))
    assert protocol._resolve_db_path() == scratch / "pool.db"


def test_legacy_path_is_used_when_it_exists(monkeypatch, scratch):
    monkeypatch.delenv("PHASE1_DB", raising=False)
    legacy = scratch / "legacy" / "phase1_protocol.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy", encoding="utf-8")
    monkeypatch.setattr(protocol, "LEGACY_DB_PATH", legacy)
    assert protocol._resolve_db_path() == legacy


def test_tempdir_fallback_without_env_and_legacy(monkeypatch, scratch):
    monkeypatch.delenv("PHASE1_DB", raising=False)
    monkeypatch.setattr(
        protocol, "LEGACY_DB_PATH", scratch / "missing" / "phase1_protocol.db"
    )
    expected = Path(tempfile.gettempdir()) / "phase1" / "phase1_protocol.db"
    assert protocol._resolve_db_path() == expected


def test_blank_env_var_is_ignored(monkeypatch, scratch):
    monkeypatch.setenv("PHASE1_DB", "   ")
    monkeypatch.setattr(
        protocol, "LEGACY_DB_PATH", scratch / "missing" / "phase1_protocol.db"
    )
    expected = Path(tempfile.gettempdir()) / "phase1" / "phase1_protocol.db"
    assert protocol._resolve_db_path() == expected


def test_export_for_ui_shares_protocol_db_path():
    assert export_for_ui.protocol is protocol
    assert export_for_ui.DEFAULT_DB == protocol.DB_PATH
    assert export_for_ui.DEFAULT_DB == protocol._resolve_db_path()


def test_get_db_creates_parent_directory(monkeypatch, scratch):
    db_path = scratch / "nested" / "dir" / "pool.db"
    monkeypatch.setattr(protocol, "DB_PATH", db_path)
    conn = protocol.get_db()
    conn.close()
    assert db_path.exists()
