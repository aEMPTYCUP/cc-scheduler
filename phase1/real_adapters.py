#!/usr/bin/env python3
"""Real worker adapters that bridge the console to external executors.

``SimulatedAdapter`` drives the state machine in-process for demos and is
blocked on a production pool (see RealPoolSimulationGuard). The adapters here
reach *real* executors and therefore set ``is_simulated = False``, which is what
lets a production pool run once one of them is wired into the supervisor.

DoubaoAdapter implements only the implement node through the agent-bridge file
mailbox (pull model):

* ``implement`` dispatches a responsibility card + DISPATCH document that the
  Mac Doubao skill claims over ssh. The console task moves to IN_PROGRESS and
  stays there - it never jumps to REVIEW, because the web Doubao works
  asynchronously and has not delivered anything yet.
* ``reconcile`` reads mailbox card state back and advances the console task
  along legal edges when the worker delivers / blocks. It is idempotent.
* acceptance / integration are refused: Doubao is coding-only by policy.

All bridge/git side effects go through injectable hooks so the state-machine
logic is unit-testable with fakes, without importing agent_bridge, touching a
real mailbox, spending tokens, or opening an ssh connection. The default hooks
lazy-import agent_bridge and are intended for a separate end-to-end check.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import protocol
from orchestrator import WorkerAdapter

# A hook receives (task dict, adapter config) and returns a bridge card dict.
DispatchHook = Callable[[Dict[str, Any], "DoubaoAdapter"], Dict[str, Any]]
# A hook receives a task id and returns the bridge card dict, or None.
CardReader = Callable[[str], Optional[Dict[str, Any]]]

# Bridge responsibility-card status -> a legal console action. Kept
# deliberately conservative; refine against the first real audit samples.
_CARD_STATUS_ACTION = {
    "awaiting_review": "SUBMITTED",   # worker delivered; acceptance not started
    "auditing": "REVIEW",            # independent auditor is running
    "needs_human": "ACCEPTED",       # audit settled PASS; awaits human integration
    "retry_requested": "REWORK",     # audit failed -> send back to the worker
    "blocked": "BLOCK",              # worker reported it cannot proceed
}
# Console statuses that mean "already handed off, do not dispatch again".
_DISPATCHED_CONSOLE = {"CLAIMED", "IN_PROGRESS", "SUBMITTED", "REVIEW",
                       "ACCEPTED", "INTEGRATION", "REWORK", "BLOCKED", "DONE"}


class RealAdapterError(RuntimeError):
    """A real adapter could not perform the requested hand-off."""


class DoubaoAdapter(WorkerAdapter):
    """Real implement-node adapter backed by the agent-bridge file mailbox."""

    is_simulated = False

    def __init__(
        self,
        *,
        bridge_root: str,
        owner: str,
        worktree_root: str,
        auditor: str = "acceptance-auditor",
        dispatch_hook: Optional[DispatchHook] = None,
        card_reader: Optional[CardReader] = None,
    ) -> None:
        self.bridge_root = str(bridge_root)
        self.owner = owner
        self.worktree_root = str(worktree_root)
        self.auditor = auditor
        self._dispatch_hook = dispatch_hook
        self._card_reader = card_reader

    # -- injection points -------------------------------------------------- #
    def _read_card(self, task_id: str) -> Optional[Dict[str, Any]]:
        if self._card_reader is not None:
            return self._card_reader(task_id)
        from agent_bridge.store import BridgeStore  # lazy: optional dependency
        store = BridgeStore(self.bridge_root)
        try:
            return store.load_task(task_id)
        except FileNotFoundError:
            return None

    def _dispatch(self, task: Dict[str, Any]) -> Dict[str, Any]:
        if self._dispatch_hook is not None:
            return self._dispatch_hook(task, self)
        # Default real hook: build a worktree, contract, card and DISPATCH file.
        # Isolated here so unit tests never run git/ssh or import agent_bridge.
        from agent_bridge.dispatcher import (  # lazy import
            build_dispatch_document, dispatch_task, write_dispatch_file,
        )
        from agent_bridge.store import BridgeStore
        from pathlib import Path
        tid = task["task_id"]
        worktree = str(Path(self.worktree_root) / f"{tid}-doubao")
        contract_path = str(Path(self.bridge_root) / "contracts" / f"contract.{tid}.json")
        Path(contract_path).parent.mkdir(parents=True, exist_ok=True)
        spec = {
            "id": tid,
            "title": task.get("title") or tid,
            "description": task.get("description") or "",
            "allowed_paths": json.loads(task.get("allowed_paths_json") or "[]"),
            "behavioral_check": task.get("behavioral_check") or "",
            "pass_condition": task.get("pass_condition") or "",
        }
        Path(contract_path).write_text(
            json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        store = BridgeStore(self.bridge_root)
        card = dispatch_task(
            store, task_id=tid, owner=self.owner, worktree=worktree,
            branch=f"doubao/{tid.lower()}-auto", contract_path=contract_path,
            allowed_paths=spec["allowed_paths"],
            behavioral_check=spec["behavioral_check"],
            pass_condition=spec["pass_condition"], title=spec["title"],
            auditor=self.auditor,
        )
        doc = build_dispatch_document(
            data_root=Path(self.bridge_root), card=card,
            allowed_paths=spec["allowed_paths"],
            behavioral_check=spec["behavioral_check"],
            pass_condition=spec["pass_condition"])
        write_dispatch_file(Path(worktree), doc)
        return card

    # -- WorkerAdapter interface ------------------------------------------ #
    def implement(self, task: Dict[str, Any], executor_id: str) -> str:
        tid = task["task_id"]
        card = self._read_card(tid)
        if card is None:
            self._dispatch(task)
            card = self._read_card(tid)
        card_status = (card or {}).get("execution", {}).get("status", "dispatched")

        current = protocol.get_task(tid)
        if current is None:
            raise RealAdapterError(f"task {tid} vanished from the console pool")
        if current["status"] not in _DISPATCHED_CONSOLE:
            # First hand-off: claim on behalf of the channel and park in progress.
            protocol.atomic_claim(tid, agent_id=executor_id, executor_id=executor_id)
            protocol.transition(
                tid, "IN_PROGRESS", executor_id,
                f"dispatched to bridge mailbox (owner={self.owner}, card={card_status})",
                actor_executor_id=executor_id,
            )
            return f"{tid} dispatched to bridge mailbox -> IN_PROGRESS (card {card_status})"
        return f"{tid} already in flight (console={current['status']}, card={card_status})"

    def accept(self, task: Dict[str, Any], executor_id: str) -> str:
        raise PermissionError("DoubaoAdapter is coding-only and cannot run acceptance")

    def integrate(self, task: Dict[str, Any], executor_id: str) -> str:
        raise PermissionError("DoubaoAdapter is coding-only and cannot integrate")


def reconcile(adapter: DoubaoAdapter, actor: str = "bridge-reconciler") -> List[Dict[str, str]]:
    """Pull bridge card state back into the console for dispatched tasks.

    Scans console tasks currently owned by the bridge hand-off and advances each
    along one legal edge based on its card. Idempotent: a task already at or past
    the target state is left untouched. Purely reads the mailbox; never writes it.
    """
    changes: List[Dict[str, str]] = []
    for task in protocol.list_tasks():
        if task["status"] not in {"IN_PROGRESS", "SUBMITTED", "REVIEW", "REWORK"}:
            continue
        card = adapter._read_card(task["task_id"])
        if not card:
            continue
        card_status = card.get("execution", {}).get("status")
        action = _CARD_STATUS_ACTION.get(card_status)
        if action is None:
            continue
        tid = task["task_id"]
        if action == "BLOCK":
            if task["status"] != "BLOCKED":
                try:
                    protocol.block_task(tid, actor, f"bridge card {card_status}")
                    changes.append({"task_id": tid, "action": "BLOCK", "card": card_status})
                except ValueError:
                    changes.append({"task_id": tid, "action": "skipped", "card": card_status})
            continue
        # Only move forward; never regress a task that already passed the edge.
        if task["status"] == action or _already_past(task["status"], action):
            continue
        try:
            protocol.transition(tid, action, actor,
                                f"reconciled from bridge card {card_status}",
                                actor_executor_id=actor)
            changes.append({"task_id": tid, "action": action, "card": card_status})
        except ValueError:
            # The console state has no legal edge to the mapped target yet
            # (e.g. REWORK is only reachable from REVIEW); skip this tick.
            changes.append({"task_id": tid, "action": "skipped", "card": card_status})
    return changes


# Console forward order used to avoid moving a task backwards during reconcile.
_FORWARD_ORDER = ["CLAIMED", "IN_PROGRESS", "SUBMITTED", "REVIEW",
                  "ACCEPTED", "INTEGRATION", "DONE"]


def _already_past(current: str, target: str) -> bool:
    try:
        return _FORWARD_ORDER.index(current) > _FORWARD_ORDER.index(target)
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Unattended headless executor: Claude Code CLI (subprocess, push model)
# --------------------------------------------------------------------------- #

def build_implement_prompt(task: Dict[str, Any]) -> str:
    """Pure prompt builder; the whitelist and no-push rules are explicit."""
    allowed = json.loads(task.get("allowed_paths_json") or "[]")
    whitelist = "\n".join(f"   - {p}" for p in allowed) or "   - （未限定白名单，按任务描述判断）"
    behavioral = (task.get("behavioral_check") or "").strip() or "（无指定检查）"
    acceptance = (task.get("acceptance_criteria") or task.get("pass_condition") or "").strip() or "（未提供）"
    return f"""任务 {task['task_id']}：{task.get('title') or ''}

## 任务描述
{task.get('description') or '（无）'}

## 验收标准
{acceptance}

## 硬约束（必须遵守）
1. 只能修改下列白名单路径，越界即违规：
{whitelist}
2. 禁止修改 tasks/queue.json；禁止 git push、git merge、git rebase。
3. 在当前 worktree 内完成，并实跑行为检查、留存输出：
   {behavioral}
4. 完成后只 git add 白名单内文件并 git commit（不 push）；最终回复给出 commit SHA 与验证结论。
5. 若无法完成，明确写出卡点与已尝试操作，绝不要伪造成功。
"""


def default_subprocess_runner(cmd: List[str], cwd: str, input_text: str, timeout: int):
    """Real subprocess call. User prompt on stdin survives the Windows .CMD shim."""
    return subprocess.run(
        cmd, cwd=cwd, input=input_text.encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
    )


def default_head_resolver(worktree: str) -> str:
    result = subprocess.run(
        ["git", "-C", worktree, "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RealAdapterError(f"cannot read HEAD in {worktree}: {result.stderr.strip()}")
    return result.stdout.strip()


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
    except (FileExistsError, _sp.CalledProcessError):
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


class ClaudeSubprocessAdapter(WorkerAdapter):
    """Real implement-node executor driving headless `claude -p` in a worktree.

    Every external effect is injectable (subprocess runner, worktree factory,
    HEAD resolver) so the state-machine logic is unit-testable without spawning
    claude. The integration node is intentionally refused: merging stays a
    human gate, and acceptance belongs to the independent reviewer node.
    """

    is_simulated = False

    def __init__(
        self,
        repo_path: str,
        *,
        runner: Optional[Callable] = None,
        worktree_factory: Optional[Callable[[Dict[str, Any]], str]] = None,
        head_resolver: Optional[Callable[[str], str]] = None,
        permission_mode: str = "acceptEdits",
        timeout: int = 1800,
        prompt_extra: str = "",
    ) -> None:
        self.repo_path = str(repo_path)
        self._runner = runner or default_subprocess_runner
        self._worktree_factory = worktree_factory
        self._head_resolver = head_resolver or default_head_resolver
        self.permission_mode = permission_mode
        self.timeout = timeout
        # Extra instructions appended to the implement prompt (e.g. the concrete
        # findings of a failed acceptance), so a rework round knows exactly
        # what to fix instead of re-deriving it.
        self.prompt_extra = prompt_extra

    def _ensure_worktree(self, task: Dict[str, Any]) -> str:
        existing = task.get("worktree_path")
        if existing and Path(existing).is_dir():
            return existing
        if self._worktree_factory is not None:
            return self._worktree_factory(task)
        return _recreate_worktree(self.repo_path, task["task_id"])

    @staticmethod
    def _load_persona(role_id: str) -> str:
        """Read persona content from the registered role_specs (no file path)."""
        with protocol.get_db() as conn:
            row = conn.execute(
                "SELECT content FROM role_specs WHERE role_id=?", (role_id,)
            ).fetchone()
        if not row:
            raise RealAdapterError(f"role spec {role_id!r} is not registered")
        return row["content"]

    def implement(self, task: Dict[str, Any], executor_id: str) -> str:
        tid = task["task_id"]
        try:
            protocol.atomic_claim(tid, agent_id=executor_id, executor_id=executor_id)
            protocol.transition(tid, "IN_PROGRESS", executor_id,
                                "claude headless adapter started",
                                actor_executor_id=executor_id)
            worktree = self._ensure_worktree(task)

            persona = self._load_persona(task["role_id"])
            persona_file = tempfile.NamedTemporaryFile(
                "w", suffix=f"-{tid}-role.md", delete=False, encoding="utf-8")
            try:
                persona_file.write(persona)
                persona_file.close()
                claude = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")
                if not claude:
                    raise RealAdapterError("claude executable not found in PATH")
                # Short flags inline; persona by file, user prompt by stdin: the
                # combination proven to survive the Windows npm .CMD shim.
                cmd = [
                    claude, "-p",
                    "--append-system-prompt-file", persona_file.name,
                    "--permission-mode", self.permission_mode,
                    "--output-format", "json",
                ]
                result = self._runner(cmd, worktree,
                                      build_implement_prompt(task) + self.prompt_extra,
                                      self.timeout)
            finally:
                Path(persona_file.name).unlink(missing_ok=True)

            returncode = getattr(result, "returncode", 1)
            if returncode != 0:
                output = getattr(result, "stdout", b"")
                raise RealAdapterError(
                    f"claude exited {returncode}: {str(output)[-400:]}"
                )

            sha = self._head_resolver(worktree)
            protocol.add_evidence(tid, "cli_log", f"claude-headless:{tid}",
                                  executor_id, sync_audit=False)
            protocol.add_evidence(tid, "commit_sha", f"commit:{sha}", executor_id,
                                  sha256=sha, sync_audit=False)
            protocol.transition(tid, "SUBMITTED", executor_id,
                                f"claude headless finished ({sha[:10]})",
                                actor_executor_id=executor_id)
            protocol.transition(tid, "REVIEW", "system",
                                "auto-moved to independent review",
                                actor_executor_id=executor_id)
            return f"{tid} implemented by headless claude -> REVIEW ({sha[:10]})"
        except Exception as exc:  # noqa: BLE001 - park the task, never fake success
            self._park_for_human(tid, executor_id, exc)
            return f"{tid} failed and parked for human: {type(exc).__name__}: {exc}"

    def _park_for_human(self, tid: str, executor_id: str, exc: Exception) -> None:
        try:
            protocol.record_failure(tid, executor_id, str(exc)[:500],
                                    "headless claude adapter failure")
        except Exception:  # noqa: BLE001 - FAILED edge may be illegal from this state
            try:
                protocol.block_task(tid, executor_id, f"adapter error: {type(exc).__name__}")
            except Exception:  # noqa: BLE001 - last resort; the worker loop must survive
                pass

    def accept(self, task: Dict[str, Any], executor_id: str) -> str:
        raise PermissionError(
            "acceptance runs on the independent reviewer node (audit_launcher), "
            "not the headless implement adapter"
        )

    def integrate(self, task: Dict[str, Any], executor_id: str) -> str:
        raise PermissionError("integration stays a human gate; the headless adapter never merges")
