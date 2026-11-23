#!/usr/bin/env python3
"""Codex real implement-node adapter — the primary real executor (R-98 / N-17).

Drives non-interactive ``codex exec`` in a dedicated worktree under a
workspace-write sandbox with approvals off via config override. Every external
effect is injectable (subprocess runner, worktree factory, HEAD resolver),
mirroring ClaudeSubprocessAdapter, so the state-machine logic is hermetic-
testable without spawning codex. Integration stays a human gate and acceptance
belongs to the independent reviewer node.
"""
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import protocol
from orchestrator import WorkerAdapter
from real_adapters import (
    RealAdapterError,
    build_implement_prompt,
    default_head_resolver,
    default_subprocess_runner,
)


def _recreate_worktree(repo_path: str, task_id: str) -> str:
    """Create the task worktree, clearing stale state left by a crashed run.

    A previous attempt can leave both the worktree directory AND its branch
    behind (git worktree add then fails with exit 255 because the branch
    exists). Recover deterministically: prune the registration, drop the stale
    branch, remove the directory, then create it once more.
    """
    import subprocess as _sp

    repo = Path(repo_path).resolve()
    target = repo.parent / f"wt-{task_id}"
    branch = f"task/{task_id}"
    try:
        return protocol.create_worktree(repo_path, task_id)
    except (FileExistsError, _sp.CalledProcessError) as exc:
        _sp.run(["git", "-C", str(repo), "worktree", "prune"],
                capture_output=True, text=True)
        _sp.run(["git", "-C", str(repo), "branch", "-D", branch],
                capture_output=True, text=True)
        if target.exists():
            _sp.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(target)],
                    capture_output=True, text=True)
            if target.exists():
                _sp.run(["cmd", "/c", "rmdir", "/s", "/q", str(target)],
                        capture_output=True, text=True)
        return protocol.create_worktree(repo_path, task_id)


class CodexAdapter(WorkerAdapter):
    """Real implement-node executor driving headless ``codex exec``.

    ``codex exec`` has no ``--ask-for-approval`` flag (that is a top-level
    option), so approvals are turned off through a config override:
    ``-c 'approval_policy="never"'``. Combined with the workspace-write sandbox
    this confines model-generated shell commands to the task worktree while
    still letting codex run the whitelisted git add/commit the prompt demands.
    """

    is_simulated = False

    def __init__(
        self,
        repo_path: str,
        *,
        runner: Optional[Callable] = None,
        worktree_factory: Optional[Callable[[Dict[str, Any]], str]] = None,
        head_resolver: Optional[Callable[[str], str]] = None,
        sandbox: str = "",
        approval: str = "never",
        model: Optional[str] = None,
        extra_config: Optional[List[str]] = None,
        timeout: int = 1800,
        prompt_extra: str = "",
        committer: Optional[Callable[[str, Dict[str, Any]], Optional[str]]] = None,
    ) -> None:
        self.repo_path = str(repo_path)
        # Sandbox mode: DSH_CODEX_SANDBOX overrides the default. On Windows the
        # restricted-token sandbox needs the Codex sandbox service accounts
        # (CodexSandboxOffline/Online); when that provisioning is broken the
        # codex CLI cannot spawn any process (CreateProcessWithLogonW failed: 2)
        # and the only working mode is "danger-full-access". Tasks still run in
        # an isolated worktree and the prompt forbids destructive git verbs.
        # .strip() matters: cmd scripts that write `set VAR=value && cmd` leave a
        # trailing space in the value, which codex rejects as an invalid
        # --sandbox mode.
        self.sandbox = (sandbox or os.environ.get("DSH_CODEX_SANDBOX")
                        or "workspace-write").strip()
        self._runner = runner or default_subprocess_runner
        self._worktree_factory = worktree_factory
        self._head_resolver = head_resolver or default_head_resolver
        self.approval = approval
        self.model = (model or os.environ.get("DSH_CODEX_MODEL") or "").strip() or None
        self._committer = committer
        self.extra_config = list(extra_config or [])
        self.timeout = timeout
        self.prompt_extra = prompt_extra
        # Windows sandbox note: the restricted-token sandbox denies
        # powershell.exe (exit 5 "access denied"); cmd.exe is the working shell
        # (verified 2026-11-20: `shell: cmd.exe, login: false` -> exit 0).
        self.windows_note = (
            "\n\n【执行环境提示（Windows）】沙箱内 powershell.exe 会被拒绝（exit 5），"
            "执行命令请使用 cmd.exe（如 shell=cmd.exe, login=false），"
            "不要用 PowerShell 管道语法。"
            "沙箱内禁止写入 .git，因此不要尝试 git add/commit——"
            "平台会在你完成后只提交白名单内的文件。\n"
        )

    def _ensure_worktree(self, task: Dict[str, Any]) -> str:
        existing = task.get("worktree_path")
        if existing and Path(existing).is_dir():
            return existing
        if self._worktree_factory is not None:
            return self._worktree_factory(task)
        return _recreate_worktree(self.repo_path, task["task_id"])

    def _build_argv(self, worktree: str, output_file: str) -> List[str]:
        """The fixed ``codex exec`` argv (without the executable path)."""
        argv = [
            "exec",
            "-C", worktree,
            "-s", self.sandbox,
            "--ephemeral",
            "--json",
            "-o", output_file,
            "-c", f'approval_policy="{self.approval}"',
        ]
        if self.model:
            argv += ["-m", self.model]
        for config in self.extra_config:
            argv += ["-c", config]
        return argv

    def implement(self, task: Dict[str, Any], executor_id: str) -> str:
        tid = task["task_id"]
        try:
            protocol.atomic_claim(tid, agent_id=executor_id, executor_id=executor_id)
            protocol.transition(tid, "IN_PROGRESS", executor_id,
                                "codex adapter started",
                                actor_executor_id=executor_id)
            worktree = self._ensure_worktree(task)

            codex = (shutil.which("codex") or shutil.which("codex.exe")
                     or shutil.which("codex.cmd"))
            if not codex:
                raise RealAdapterError("codex executable not found in PATH")

            output_file = str(Path(tempfile.gettempdir()) / f"codex-last-{tid}.txt")
            cmd = [codex] + self._build_argv(worktree, output_file)
            result = self._runner(cmd, worktree,
                                  build_implement_prompt(task) + self.prompt_extra
                                  + self.windows_note,
                                  self.timeout)

            returncode = getattr(result, "returncode", 1)
            if returncode != 0:
                output = getattr(result, "stdout", b"")
                raise RealAdapterError(
                    f"codex exited {returncode}: {str(output)[-400:]}"
                )

            # The Windows sandbox denies writes to .git, so the executor cannot
            # commit itself; the platform commits the whitelisted paths (never
            # git add -A). Whitelist-only staging is a platform red line.
            committed = self._committer(worktree, task) if self._committer else None
            sha = committed or self._head_resolver(worktree)
            protocol.add_evidence(tid, "cli_log", f"codex-headless:{tid}",
                                  executor_id, sync_audit=False)
            protocol.add_evidence(tid, "commit_sha", f"commit:{sha}", executor_id,
                                  sha256=sha, sync_audit=False)
            protocol.transition(tid, "SUBMITTED", executor_id,
                                f"codex headless finished ({sha[:10]})",
                                actor_executor_id=executor_id)
            protocol.transition(tid, "REVIEW", "system",
                                "auto-moved to independent review",
                                actor_executor_id=executor_id)
            return f"{tid} implemented by headless codex -> REVIEW ({sha[:10]})"
        except Exception as exc:  # noqa: BLE001 - park the task, never fake success
            self._park_for_human(tid, executor_id, exc)
            return f"{tid} failed and parked for human: {type(exc).__name__}: {exc}"

    def accept(self, task: Dict[str, Any], executor_id: str) -> str:
        raise PermissionError("CodexAdapter is coding-only and cannot run acceptance")

    def integrate(self, task: Dict[str, Any], executor_id: str) -> str:
        raise PermissionError("CodexAdapter is coding-only and cannot integrate")

    def _park_for_human(self, tid: str, executor_id: str, exc: Exception) -> None:
        try:
            protocol.record_failure(tid, executor_id, str(exc)[:500],
                                    "codex adapter failure")
        except Exception:  # noqa: BLE001 - FAILED edge may be illegal from this state
            try:
                protocol.block_task(tid, executor_id,
                                    f"adapter error: {type(exc).__name__}")
            except Exception:  # noqa: BLE001 - last resort; the worker loop must survive
                pass


def main(argv: Optional[List[str]] = None) -> int:
    """Standalone entry: drive one READY task to REVIEW via headless codex.

    ``python codex_adapter.py --task T-XXX --repo C:/path/to/repo``
    """
    parser = argparse.ArgumentParser(description="Implement one task via headless codex exec")
    parser.add_argument("--task", required=True, help="task_id to implement")
    parser.add_argument("--repo", required=True, help="repository path used for worktrees")
    parser.add_argument("--executor", default="codex", help="executor_id (default codex)")
    parser.add_argument("--model", default=None, help="codex model id")
    parser.add_argument("--sandbox", default="workspace-write",
                        help="codex sandbox (read-only|workspace-write|danger-full-access)")
    parser.add_argument("--timeout", type=int, default=1800, help="subprocess timeout seconds")
    args = parser.parse_args(argv)

    task = protocol.get_task(args.task)
    if task is None:
        raise SystemExit(f"task {args.task} not found in the console pool")
    adapter = CodexAdapter(args.repo, sandbox=args.sandbox, model=args.model,
                           timeout=args.timeout)
    summary = adapter.implement(task, args.executor)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
