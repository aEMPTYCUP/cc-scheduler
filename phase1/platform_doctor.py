#!/usr/bin/env python3
"""Platform doctor — one-shot environment preflight for the whole task platform.

Every failure we hit today was an environment/integration contract that was
discovered reactively (executor not logged in, git worktree left behind by a
crashed run, sandbox service stopped, PAT without workflow scope, bridge
mailbox unwritable, DB schema missing...). This module checks all of them in
one pass so they are found BEFORE a task is dispatched.

Read-only by design: it inspects, it never mutates production state.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

PHASE1_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PHASE1_DIR))

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", fix: str = "", warn_only: bool = False) -> bool:
        status = PASS if ok else (WARN if warn_only else FAIL)
        self.checks.append(Check(name, status, detail, fix))
        return ok

    def failed(self) -> List[Check]:
        return [c for c in self.checks if c.status == FAIL]

    def warned(self) -> List[Check]:
        return [c for c in self.checks if c.status == WARN]

    def to_dict(self) -> Dict:
        return {
            "ok": not self.failed(),
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}
                for c in self.checks
            ],
            "failed": len(self.failed()),
            "warned": len(self.warned()),
        }


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #

def check_python(report: Report) -> None:
    report.add("python runtime", sys.version_info >= (3, 9),
               f"{sys.version.split()[0]}")


def check_git(report: Report, repo_path: Optional[str]) -> None:
    git = shutil.which("git")
    report.add("git on PATH", bool(git), git or "not found",
               "install git and add it to PATH")
    if not git or not repo_path:
        return
    repo = Path(repo_path)
    is_repo = (repo / ".git").exists()
    report.add("target repo is a git repo", is_repo, str(repo),
               "point --repo at the real repository")
    if not is_repo:
        return
    try:
        out = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        report.add("git worktree list works", out.returncode == 0,
                   f"{len(out.stdout.splitlines())} worktrees",
                   "run 'git worktree prune' to clear stale registrations")
    except Exception as exc:  # noqa: BLE001
        report.add("git worktree list works", False, str(exc)[:120])


def check_stale_worktrees(report: Report, repo_path: Optional[str]) -> None:
    """A crashed run leaves a worktree AND its branch behind; git worktree add
    then fails with exit 255. Detect it before dispatch."""
    if not repo_path:
        return
    repo = Path(repo_path)
    if not (repo / ".git").exists():
        return
    try:
        out = subprocess.run(["git", "-C", str(repo), "branch", "--list", "task/*"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        stale, active = [], []
        for raw in out.stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            # "+" means the branch is checked out in another worktree (legit).
            if line.startswith("+"):
                active.append(line.lstrip("+ ").strip())
            else:
                stale.append(line.lstrip("* ").strip())
        report.add("no stale task/* branches", not stale,
                   f"stale: {stale[:5]}" if stale else
                   (f"{len(active)} active worktree branch(es)" if active else "clean"),
                   "delete stale branches (git branch -D <b>) or let the adapter recreate them",
                   warn_only=True)
    except Exception as exc:  # noqa: BLE001
        report.add("no stale task/* branches", False, str(exc)[:120], warn_only=True)


# Install locations that are not always on a non-interactive session's PATH.
_EXTRA_BIN_DIRS = (
    r"C:\Users\32831\.local\node\bin",
    r"C:\Users\32831\.local\bin",
    r"C:\Users\32831\AppData\Roaming\npm",
)


def _find_binary(binary: str) -> Optional[str]:
    path = shutil.which(binary)
    if path:
        return path
    for folder in _EXTRA_BIN_DIRS:
        for suffix in (".cmd", ".exe", ".bat", ""):
            candidate = Path(folder) / f"{binary}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def check_executor(report: Report, name: str, binary: str, probe_cmd: Optional[List[str]] = None,
                   env_var: Optional[str] = None) -> None:
    """Presence + (cheap) usability of an external executor CLI."""
    path = _find_binary(binary)
    report.add(f"{name} CLI present", bool(path), path or f"{binary} not on PATH",
               f"install {binary} or add it to PATH")
    if not path:
        return
    if env_var:
        value = os.environ.get(env_var)
        report.add(f"{name} credential present ({env_var})", bool(value),
                   "set" if value else "missing",
                   f"export {env_var}=...", warn_only=True)
    if probe_cmd:
        try:
            # On Windows the npm shims are .CMD files: they need shell=True
            # (or an explicit .exe) or subprocess raises WinError 2.
            proc = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
                                  shell=(os.name == "nt"))
            out = (proc.stdout + proc.stderr).lower()
            broken = "createprocesswithlogonw failed" in out or "sandbox failed" in out
            report.add(f"{name} can spawn a process", proc.returncode == 0 and not broken,
                       "probe ok" if proc.returncode == 0 else out.strip()[:160],
                       "the Windows restricted-token sandbox is broken; set "
                       "DSH_CODEX_SANDBOX=danger-full-access or repair the Codex "
                       "sandbox service accounts",
                       warn_only=True)
        except Exception as exc:  # noqa: BLE001
            report.add(f"{name} can spawn a process", False, str(exc)[:120],
                       "probe failed", warn_only=True)


def check_codex_sandbox_mode(report: Report) -> None:
    mode = os.environ.get("DSH_CODEX_SANDBOX", "workspace-write")
    ok = mode in ("workspace-write", "read-only", "danger-full-access")
    report.add("DSH_CODEX_SANDBOX valid", ok, mode,
               "use read-only | workspace-write | danger-full-access")


def check_doubao_identities(report: Report, bridge_root: str) -> None:
    """The two controlled Doubao accounts must be registered in the bridge."""
    registry = Path(bridge_root) / "agents" / "registry.json"
    if not registry.is_file():
        report.add("doubao identities registered", False, f"missing {registry}",
                   "run the identity registry CLI to register the two accounts")
        return
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        report.add("doubao identities registered", False, str(exc)[:120],
                   "repair the registry file")
        return
    blob = json.dumps(data, ensure_ascii=False)
    wanted = ["doubao-shenlanking", "doubao-woaijiangjiang"]
    missing = [w for w in wanted if w not in blob]
    report.add("doubao identities registered", not missing,
               f"missing: {missing}" if missing else "both accounts registered",
               "register the missing identity in the bridge registry")


def check_codex_sandbox_functional(report: Report) -> None:
    """Deep probe: can the Codex Windows sandbox actually run a command?

    Root cause found 2026-11-20: the Windows sandbox only works from an
    INTERACTIVE, ELEVATED session. Over SSH (non-interactive) the runner pipe
    handshake times out; without elevation the setup cannot grant write ACEs
    (os error 5). A plain probe in this process therefore reports a false
    failure -- the honest check is the scheduled-task probe that runs in the
    interactive session (DSH-CodexProbe / DSH-CodexWorker).
    """
    marker = Path(tempfile.gettempdir()) / "codex-sandbox-probe-result.txt"
    if marker.is_file():
        marker.unlink()
    task = "DSH-CodexProbe"
    cmd = (f'cmd /c codex sandbox cmd.exe /c echo SANDBOX-OK > "{marker}" 2>&1')
    try:
        subprocess.run(["schtasks", "/delete", "/tn", task, "/f"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        created = subprocess.run(
            ["schtasks", "/create", "/tn", task, "/tr", cmd,
             "/sc", "once", "/st", "23:59", "/it", "/ru", "32831", "/f"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if "SUCCESS" not in (created.stdout + created.stderr).upper():
            report.add("codex windows sandbox functional", False,
                       "could not create the interactive probe task",
                       "run the probe manually from an elevated terminal",
                       warn_only=True)
            return
        subprocess.run(["schtasks", "/run", "/tn", task],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        for _ in range(30):
            time.sleep(2)
            if marker.is_file():
                break
        out = marker.read_text(encoding="utf-8", errors="ignore") if marker.is_file() else ""
        ok = "SANDBOX-OK" in out
        report.add("codex windows sandbox functional", ok,
                   "sandbox ran the command (interactive+elevated)" if ok else out.strip()[:140],
                   "the sandbox needs an INTERACTIVE and ELEVATED session "
                   "(DSH-CodexWorker task); keep DSH_CODEX_SANDBOX=workspace-write "
                   "and run the worker through that task",
                   warn_only=True)
    except Exception as exc:  # noqa: BLE001
        report.add("codex windows sandbox functional", False, str(exc)[:140],
                   "run the probe manually", warn_only=True)
    finally:
        subprocess.run(["schtasks", "/delete", "/tn", task, "/f"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)


def check_bridge_mailbox(report: Report, bridge_root: str) -> None:
    root = Path(bridge_root)
    exists = root.is_dir()
    report.add("bridge data root exists", exists, str(root),
               "set --bridge-root to the live bridge-live directory")
    if not exists:
        return
    buckets = ["tasks", "inbox", "audit-requests", "audit-results", "dead-letter",
               "completed", "notifications"]
    missing = [b for b in buckets if not (root / b).is_dir()]
    report.add("bridge buckets present", not missing,
               f"missing: {missing}" if missing else f"{len(buckets)} buckets",
               "create the missing bucket directories")
    try:
        probe = root / ".doctor-write-probe"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        report.add("bridge mailbox writable", True, "ok")
    except Exception as exc:  # noqa: BLE001
        report.add("bridge mailbox writable", False, str(exc)[:120],
                   "fix permissions on the bridge data root")


def check_console_db(report: Report, db_path: str) -> None:
    db = Path(db_path)
    exists = db.is_file()
    report.add("console pool DB exists", exists, str(db),
               "set PHASE1_DB / --db to the real console pool")
    if not exists:
        return
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        needed = {"tasks", "audit_log", "role_specs"}
        missing = sorted(needed - tables)
        report.add("console DB schema complete", not missing,
                   f"missing tables: {missing}" if missing else "schema ok",
                   "run protocol.ensure_schema() (or cli.py init)")
        seq = None
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT value FROM pool_meta WHERE key='bridge_outbox_seq'").fetchone()
        conn.close()
        seq = row[0] if row else None
        report.add("bridge projection cursor present", seq is not None,
                   f"seq={seq}" if seq else "pool_meta has no bridge_outbox_seq",
                   "run bridge_sync.sync_once() once to create it", warn_only=True)
    except Exception as exc:  # noqa: BLE001
        report.add("console DB readable", False, str(exc)[:120])


def check_scheduled_tasks(report: Report) -> None:
    if os.name != "nt":
        return
    for task, want_running in (("DSH-SyncWatch", True), ("AgentBridgeWorker", False)):
        try:
            proc = subprocess.run(["schtasks", "/query", "/tn", task, "/fo", "LIST"],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            out = proc.stdout
            enabled = "Disabled" not in out
            if want_running:
                report.add(f"scheduled task {task} enabled", enabled,
                           "enabled" if enabled else "disabled",
                           "re-create/enable the task", warn_only=True)
            else:
                report.add(f"scheduled task {task} disabled (futures stopped)", not enabled,
                           "disabled" if not enabled else "still enabled",
                           "disable it while the futures track is parked", warn_only=True)
        except Exception as exc:  # noqa: BLE001
            report.add(f"scheduled task {task} queryable", False, str(exc)[:120],
                       warn_only=True)


def check_credentials(report: Report) -> None:
    """Presence only — never print a secret."""
    git_cred = Path(os.path.expanduser("~/.git-credentials"))
    has_cred = git_cred.is_file()
    report.add("git credential file present", has_cred,
               "~/.git-credentials" if has_cred else "missing",
               "configure a PAT or use SSH remotes (SSH needs no workflow scope)",
               warn_only=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Platform environment preflight")
    parser.add_argument("--repo", default=os.environ.get("DSH_REPO_PATH"),
                        help="target repository for worktrees")
    parser.add_argument("--bridge-root", default=os.environ.get(
        "DSH_BRIDGE_ROOT", r"C:/Users/32831/wt/bridge-live"))
    parser.add_argument("--db", default=os.environ.get(
        "PHASE1_DB", r"C:/Users/32831/cc-scheduler/phase1/.local/queue.db"))
    parser.add_argument("--skip-executors", action="store_true",
                        help="do not probe external CLIs (fast)")
    parser.add_argument("--deep", action="store_true",
                        help="also probe the codex Windows sandbox (slow: ~30s when broken)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = Report()
    check_python(report)
    check_git(report, args.repo)
    check_stale_worktrees(report, args.repo)
    check_codex_sandbox_mode(report)
    if not args.skip_executors:
        check_executor(report, "codex", "codex", probe_cmd=["codex", "--version"])
        check_executor(report, "claude", "claude", probe_cmd=["claude", "--version"])
        if os.name == "nt":
            # The Doubao CLI is a Mac-side asset; on Windows the bridge talks to
            # the two Doubao desktop accounts through the file mailbox, so the
            # meaningful check is the registered identities.
            check_doubao_identities(report, args.bridge_root)
        else:
            check_executor(report, "doubao", "doubao", probe_cmd=["doubao", "--version"])
    if args.deep:
        check_codex_sandbox_functional(report)
    check_bridge_mailbox(report, args.bridge_root)
    check_console_db(report, args.db)
    check_scheduled_tasks(report)
    check_credentials(report)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        for c in report.checks:
            mark = {PASS: "OK  ", WARN: "WARN", FAIL: "FAIL"}[c.status]
            line = f"{mark} {c.name}"
            if c.detail:
                line += f" — {c.detail}"
            print(line)
            if c.status != PASS and c.fix:
                print(f"     fix: {c.fix}")
        print("-" * 60)
        print(f"failed={len(report.failed())} warned={len(report.warned())} "
              f"total={len(report.checks)}")
    return 1 if report.failed() else 0


if __name__ == "__main__":
    raise SystemExit(main())
