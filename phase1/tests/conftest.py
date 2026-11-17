#!/usr/bin/env python3
r"""Shared pytest configuration for the Phase 1 protocol test-suite.

Two jobs:

1. Make "import protocol" work no matter which directory pytest is started
   from (mirrors the sys.path handling already used by test_db_path.py).
2. Move pytest's temp root out of the shared "pytest-of-<user>" folder. On
   this machine those folders were left behind by another account and cannot
   be listed, which makes pytest die with a PermissionError even though every
   assertion passed. Pointing "basetemp" at a private directory under the
   system temp dir avoids them entirely.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

PHASE1_DIR = Path(__file__).resolve().parents[1]
if str(PHASE1_DIR) not in sys.path:
    sys.path.insert(0, str(PHASE1_DIR))

import protocol  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    """Use a private pytest temp root unless the caller asked for one."""
    if config.option.basetemp is not None:
        return

    basetemp = Path(tempfile.gettempdir()) / "ccs-pytest-basetemp"
    config.option.basetemp = basetemp

    # This conftest is only an "initial conftest" (and therefore configured
    # before pytest builds its temp factory) when the tests are named on the
    # command line. For a bare "pytest" run it is loaded later, during
    # collection, and config.option.basetemp would be ignored. The factory
    # resolves its base temp dir lazily, so patching it here still works.
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None and factory._given_basetemp is None:
        factory._given_basetemp = basetemp


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated DB path for a single test.

    protocol.DB_PATH is redirected into pytest's own tmp_path, so no test
    ever touches the real /tmp, C:\tmp or the user home directory.
    """
    path = tmp_path / "pool.db"
    monkeypatch.setattr(protocol, "DB_PATH", path)
    # atomic_claim() writes a task package into PACKAGES_DIR; keep that
    # inside tmp_path too so a test run leaves nothing in the repository.
    monkeypatch.setattr(protocol, "PACKAGES_DIR", tmp_path / "task-packages")
    return path
