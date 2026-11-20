#!/usr/bin/env python3
"""Hermetic tests for the headless Codex adapter (no real codex)."""
from __future__ import annotations

from pathlib import Path

import pytest

import protocol
from codex_adapter import CodexAdapter
from real_adapters import build_implement_prompt


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


def _ready_task(tid="T-710", *, allowed=None):
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
    return CodexAdapter(
        repo_path=str(tmp_path / "repo"),
        runner=runner,
        worktree_factory=lambda task: str(tmp_path / "wt"),
        head_resolver=lambda worktree: "a" * 40,
        **kw,
    )


def test_is_real_and_prompt_carries_whitelist_and_no_push(db_path):
    # R-98: codex is the PRIMARY real adapter; it must be a WorkerAdapter and
    # never simulated, and its prompt must carry the same whitelist/no-push
    # discipline as the claude adapter.
    assert CodexAdapter.__mro__[1].__name__ == "WorkerAdapter"
    assert CodexAdapter.is_simulated is False
    tid = _ready_task()
    task = protocol.get_task(tid)
    prompt = build_implement_prompt(task)
    assert "backend/app/x.py" in prompt
    assert "禁止 git push" in prompt
    assert "tasks/queue.json" in prompt


def test_implement_happy_path_drives_to_review_with_evidence(db_path, tmp_path):
    runner = FakeRunner()
    adapter = _adapter(tmp_path, runner)
    tid = _ready_task()
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)

    summary = adapter.implement(protocol.get_task(tid), "codex")
    assert "REVIEW" in summary
    call = runner.calls[0]
    # Non-interactive codex exec: sandboxed workspace, approvals off via config
    # override (exec has no -a flag), JSON events, ephemeral session, prompt on
    # stdin (the runner passes input_text).
    argv = call["cmd"]
    assert all(isinstance(part, str) for part in argv), "argv must be plain strings"
    # argv[0] is the resolved executable path (codex.CMD on Windows), argv[1] is exec.
    assert Path(argv[0]).name.lower().startswith("codex")
    assert argv[1] == "exec"
    assert "-C" in argv and call["cwd"].endswith("wt")
    assert "-s" in argv and argv[argv.index("-s") + 1] == "workspace-write"
    assert "--ephemeral" in argv
    assert "--json" in argv
    assert "-o" in argv
    assert "approval_policy=\"never\"" in argv
    assert "backend/app/x.py" in call["prompt"]

    task = protocol.get_task(tid)
    assert task["status"] == "REVIEW"
    assert task["implementer_executor_id"] == "codex"
    kinds = {r[0] for r in protocol.get_db().__enter__().execute(
        "SELECT kind FROM task_evidence WHERE task_id=?", (tid,)).fetchall()}
    assert {"cli_log", "commit_sha"} <= kinds


def test_implement_model_and_extra_config_are_injected(db_path, tmp_path):
    runner = FakeRunner()
    adapter = _adapter(tmp_path, runner, model="gpt-5.2-codex",
                       extra_config=['model_provider="openai"', "max_turns=5"])
    tid = _ready_task()
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)
    adapter.implement(protocol.get_task(tid), "codex")
    argv = runner.calls[0]["cmd"]
    assert "-m" in argv and argv[argv.index("-m") + 1] == "gpt-5.2-codex"
    # extra_config entries arrive as separate argv elements after a -c flag.
    assert 'model_provider="openai"' in argv
    assert "max_turns=5" in argv


def test_implement_nonzero_exit_parks_for_human_without_faking_success(db_path, tmp_path):
    # R-133: a nonzero exit must NOT be turned into a fake success; the task is
    # parked (record_failure) and the adapter reports the failure.
    runner = FakeRunner(result=FakeResult(returncode=7, stdout=b"boom"))
    adapter = _adapter(tmp_path, runner)
    tid = _ready_task()
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)

    summary = adapter.implement(protocol.get_task(tid), "codex")
    assert "failed and parked" in summary
    assert "codex exited 7" in summary

    task = protocol.get_task(tid)
    assert task["status"] == "FAILED"
    assert task.get("last_error") and "codex exited 7" in task["last_error"]
    # Never advanced to REVIEW.
    assert "REVIEW" not in summary


def test_accept_and_integrate_are_refused(db_path, tmp_path):
    adapter = _adapter(tmp_path, FakeRunner())
    with pytest.raises(PermissionError):
        adapter.accept({}, "codex")
    with pytest.raises(PermissionError):
        adapter.integrate({}, "codex")
