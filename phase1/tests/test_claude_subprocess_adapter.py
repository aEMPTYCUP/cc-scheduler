#!/usr/bin/env python3
"""Hermetic tests for the headless Claude subprocess adapter (no real claude)."""
from __future__ import annotations

from pathlib import Path

import pytest

import orchestrator
import protocol
from real_adapters import ClaudeSubprocessAdapter, build_implement_prompt


class FakeResult:
    def __init__(self, returncode=0, stdout=b"ok"):
        self.returncode = returncode
        self.stdout = stdout


class FakeRunner:
    def __init__(self, result=FakeResult()):
        self.result = result
        self.calls = []

    def __call__(self, cmd, cwd, input_text, timeout):
        self.calls.append({"cmd": cmd, "cwd": cwd, "prompt": input_text, "timeout": timeout})
        return self.result


def _ready_task(tid="T-700", *, allowed=None):
    protocol.ensure_schema()
    with protocol.get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO role_specs (role_id, name, content_hash, content, created_at) "
            "VALUES ('r1','r1','h','c',?)", (protocol.utcnow(),))
        conn.commit()
    protocol.create_task(
        tid, "标题", "描述", "r1", "验收", priority="high",
        allowed_paths=allowed or ["backend/app/x.py"],
        behavioral_check="pytest -k x",
    )
    protocol.ready_task(tid)
    return tid


def _adapter(tmp_path, runner, **kw):
    return ClaudeSubprocessAdapter(
        repo_path=str(tmp_path / "repo"),
        runner=runner,
        worktree_factory=lambda task: str(tmp_path / "wt"),
        head_resolver=lambda worktree: "a" * 40,
        **kw,
    )


def test_is_real_and_prompt_carries_whitelist_and_no_push(db_path):
    assert ClaudeSubprocessAdapter.__mro__[1].__name__ == "WorkerAdapter"
    tid = _ready_task()
    task = protocol.get_task(tid)
    prompt = build_implement_prompt(task)
    assert "backend/app/x.py" in prompt
    assert "禁止 git push" in prompt
    assert "tasks/queue.json" in prompt
    assert ClaudeSubprocessAdapter.is_simulated is False


def test_implement_happy_path_drives_to_review_with_evidence(db_path, tmp_path):
    runner = FakeRunner()
    adapter = _adapter(tmp_path, runner)
    tid = _ready_task()
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)

    summary = adapter.implement(protocol.get_task(tid), "claude")
    assert "REVIEW" in summary
    call = runner.calls[0]
    # Windows-safe invocation: persona by file *path string* (not the temp-file
    # wrapper object), json output, prompt delivered on stdin.
    assert all(isinstance(part, str) for part in call["cmd"]), "argv must be plain strings"
    assert "--append-system-prompt-file" in call["cmd"]
    persona_arg = call["cmd"][call["cmd"].index("--append-system-prompt-file") + 1]
    assert persona_arg.endswith(".md") and isinstance(persona_arg, str)
    assert "--output-format" in call["cmd"] and "json" in call["cmd"]
    assert "backend/app/x.py" in call["prompt"]
    assert call["cwd"].endswith("wt")

    task = protocol.get_task(tid)
    assert task["status"] == "REVIEW"
    assert task["implementer_executor_id"] == "claude"
    kinds = {r[0] for r in protocol.get_db().__enter__().execute(
        "SELECT kind FROM task_evidence WHERE task_id=?", (tid,)).fetchall()}
    assert {"cli_log", "commit_sha"} <= kinds


def test_implement_failure_parks_task_and_never_fakes_review(db_path, tmp_path):
    runner = FakeRunner(FakeResult(returncode=1, stdout=b"boom"))
    adapter = _adapter(tmp_path, runner)
    tid = _ready_task()
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)

    summary = adapter.implement(protocol.get_task(tid), "claude")  # must not raise
    assert "parked for human" in summary
    assert protocol.get_task(tid)["status"] in {"FAILED", "BLOCKED"}
    assert runner.calls  # claude was actually attempted


def test_accept_and_integrate_are_refused(db_path, tmp_path):
    adapter = _adapter(tmp_path, FakeRunner())
    tid = _ready_task()
    with pytest.raises(PermissionError):
        adapter.accept(protocol.get_task(tid), "claude")
    with pytest.raises(PermissionError):
        adapter.integrate(protocol.get_task(tid), "claude")


def test_production_runs_headless_adapter_through_worker(db_path, tmp_path, monkeypatch):
    orchestrator.ensure_orchestrator()
    protocol.set_pool_mode("production")
    monkeypatch.setattr(orchestrator, "_active_adapter_simulated", False)
    runner = FakeRunner()
    adapter = _adapter(tmp_path, runner)
    tid = _ready_task()
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)

    assert orchestrator.start_worker("claude", "implement")["running"] is True  # not 403-guarded
    try:
        once = orchestrator.run_worker_once("claude", "implement", adapter)
        assert once["status"] == "worked"
        assert protocol.get_task(tid)["status"] == "REVIEW"
    finally:
        orchestrator.stop_worker("claude", "implement")


def test_implement_session_preapproves_tools_and_denies_dangerous_git(db_path, tmp_path):
    # Headless "claude -p" auto-denies every Bash call unless the tool is
    # pre-approved: the agent then loops and exits non-zero (measured
    # 2026-11-23 on the chart plan-link M0 task).
    import json

    from real_adapters import IMPL_ALLOWED_TOOLS, IMPL_DENY_RULES, write_impl_settings

    runner = FakeRunner()
    adapter = _adapter(tmp_path, runner)
    tid = _ready_task("T-760")
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)
    adapter.implement(protocol.get_task(tid), "claude")

    cmd = runner.calls[0]["cmd"]
    assert "--allowedTools" in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert allowed == IMPL_ALLOWED_TOOLS
    assert "Bash" in allowed and "Write" in allowed and "Edit" in allowed
    assert "--settings" in cmd
    settings_path = cmd[cmd.index("--settings") + 1]
    # The settings file is deleted after the run, so read it from the caller's
    # perspective: it must have existed (the CLI needs it) and carried the deny
    # rules. Re-create it to assert the content.
    fresh = write_impl_settings()
    try:
        permissions = json.loads(Path(fresh).read_text(encoding="utf-8"))["permissions"]
        assert set(permissions["allow"]) == {"Bash", "Read", "Write", "Edit", "Grep", "Glob"}
        for rule in ("Bash(git push:*)", "Bash(git merge:*)", "Bash(rm:*)"):
            assert rule in permissions["deny"]
        assert set(permissions["deny"]) == set(IMPL_DENY_RULES)
    finally:
        Path(fresh).unlink(missing_ok=True)
