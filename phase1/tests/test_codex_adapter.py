#!/usr/bin/env python3
"""Hermetic tests for the headless Codex adapter (no real codex)."""
from __future__ import annotations

import subprocess
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


def test_main_requires_task_and_repo():
    from codex_adapter import main
    with pytest.raises(SystemExit):
        main([])


def test_main_unknown_task_exits(db_path):
    from codex_adapter import main
    protocol.ensure_schema()
    with pytest.raises(SystemExit):
        main(["--task", "T-NOPE", "--repo", "x"])


def test_main_drives_implement_with_codex_executor(db_path, tmp_path, monkeypatch):
    from codex_adapter import CodexAdapter, main
    tid = _ready_task("T-711")
    calls = []

    def fake_implement(self, task, executor_id):
        calls.append((task["task_id"], executor_id))
        return "T-711 implemented by headless codex -> REVIEW"

    monkeypatch.setattr(CodexAdapter, "implement", fake_implement)
    rc = main(["--task", "T-711", "--repo", str(tmp_path / "repo"), "--executor", "codex"])
    assert rc == 0
    assert calls == [("T-711", "codex")]


def test_sandbox_mode_is_env_overridable(monkeypatch, tmp_path):
    # The Windows restricted-token sandbox depends on the Codex sandbox
    # service accounts; when that provisioning is broken the CLI cannot spawn
    # processes at all, so the mode must be overridable without code changes.
    monkeypatch.delenv("DSH_CODEX_SANDBOX", raising=False)
    adapter = CodexAdapter(repo_path=str(tmp_path / "repo"))
    assert adapter.sandbox == "workspace-write"
    monkeypatch.setenv("DSH_CODEX_SANDBOX", "danger-full-access")
    adapter = CodexAdapter(repo_path=str(tmp_path / "repo"))
    assert adapter.sandbox == "danger-full-access"
    # An explicit argument still wins over the environment.
    adapter = CodexAdapter(repo_path=str(tmp_path / "repo"), sandbox="read-only")
    assert adapter.sandbox == "read-only"


def test_stale_worktree_is_pruned_and_retried(db_path, tmp_path, monkeypatch):
    # A crashed previous run leaves the worktree behind; the adapter must clean
    # the stale state and retry instead of failing the task on FileExistsError.
    import codex_adapter
    import protocol

    calls = []
    git_calls = []

    def fake_create(repo_path, task_id, base_ref="HEAD"):
        calls.append(task_id)
        if len(calls) == 1:
            raise FileExistsError("Worktree already exists")
        return str(tmp_path / "wt")

    def fake_run(cmd, *args, **kwargs):
        git_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(protocol, "create_worktree", fake_create)
    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = codex_adapter.CodexAdapter(repo_path=str(tmp_path / "repo"))
    task = {"task_id": "T-STALE", "worktree_path": None}
    assert adapter._ensure_worktree(task).endswith("wt")
    assert calls == ["T-STALE", "T-STALE"]
    # Cleanup ran: prune + branch delete (+ worktree remove when present).
    assert any("worktree" in " ".join(c) and "prune" in " ".join(c) for c in git_calls)
    assert any("branch" in " ".join(c) and "-D" in " ".join(c) for c in git_calls)


def test_stale_branch_is_cleared_and_worktree_recreated(db_path, tmp_path, monkeypatch):
    # A crashed run can leave the BRANCH behind too: git worktree add then
    # exits 255 ("branch already exists"). The adapter must prune the
    # registration, drop the branch and recreate deterministically.
    import subprocess

    import codex_adapter

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init", "--no-verify"],
                   check=True, capture_output=True)
    # Stale branch left behind by an earlier crashed run.
    subprocess.run(["git", "-C", str(repo), "branch", "task/T-BRANCH"],
                   check=True, capture_output=True)
    protocol.ensure_schema()

    adapter = codex_adapter.CodexAdapter(repo_path=str(repo))
    path = codex_adapter._recreate_worktree(str(repo), "T-BRANCH")
    assert Path(path).is_dir()
    # The stale branch was replaced by a fresh worktree branch.
    out = subprocess.run(["git", "-C", str(repo), "branch", "--list", "task/T-BRANCH"],
                         capture_output=True, text=True)
    assert "task/T-BRANCH" in out.stdout


def test_prompt_tells_codex_to_use_cmd_shell(db_path, tmp_path):
    # Verified 2026-11-20: inside the Windows sandbox powershell.exe is denied
    # (exit 5) while cmd.exe works. The adapter must tell codex this.
    runner = FakeRunner()
    adapter = _adapter(tmp_path, runner)
    tid = _ready_task("T-720")
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)
    adapter.implement(protocol.get_task(tid), "codex")
    prompt = runner.calls[0]["prompt"]
    assert "cmd.exe" in prompt
    assert "powershell" in prompt.lower()


def test_sandbox_env_value_is_stripped(monkeypatch, tmp_path):
    # cmd scripts produce `set VAR=value && ...` -> the value keeps a trailing
    # space, which codex rejects as an invalid --sandbox mode.
    monkeypatch.setenv("DSH_CODEX_SANDBOX", "workspace-write ")
    adapter = CodexAdapter(repo_path=str(tmp_path / "repo"))
    assert adapter.sandbox == "workspace-write"
    monkeypatch.setenv("DSH_CODEX_SANDBOX", " danger-full-access\n")
    adapter = CodexAdapter(repo_path=str(tmp_path / "repo"))
    assert adapter.sandbox == "danger-full-access"


def test_platform_commits_whitelisted_paths_when_executor_cannot(db_path, tmp_path):
    # The Windows sandbox denies .git writes, so the executor cannot commit;
    # the platform commits the whitelisted paths itself (never git add -A).
    import subprocess

    commits = []

    def fake_committer(worktree, task):
        commits.append((worktree, task["task_id"]))
        return "f" * 40

    runner = FakeRunner()
    adapter = CodexAdapter(repo_path=str(tmp_path / "repo"), runner=runner,
                           worktree_factory=lambda task: str(tmp_path / "wt"),
                           head_resolver=lambda wt: "a" * 40,
                           committer=fake_committer)
    tid = _ready_task("T-730")
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)
    adapter.implement(protocol.get_task(tid), "codex")
    assert commits and commits[0][1] == "T-730"
    assert protocol.get_task(tid)["status"] == "REVIEW"
