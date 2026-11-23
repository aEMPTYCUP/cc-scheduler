#!/usr/bin/env python3
"""Integrate accepted task branches into master, without touching the dirt.

The main repo working tree carries hundreds of uncommitted files (the user's
own in-progress work plus task evidence). A plain ``git merge`` there would
either refuse or risk the working tree, so the integration runs in a separate
clean worktree and lands on master with a fast-forward only:

  1. merge every branch into the integration worktree (detached on master);
  2. report any conflict and stop -- never auto-resolve;
  3. on the main repo, stash ONLY the files that both the incoming change and
     the local working tree touch, fast-forward, then restore the stash;
  4. advance the console task state ACCEPTED -> INTEGRATION -> DONE.

Default is a dry run: pass --apply to touch anything.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PHASE1 = Path(__file__).resolve().parents[1]
if str(PHASE1) not in sys.path:
    sys.path.insert(0, str(PHASE1))

import protocol  # noqa: E402

REPO = r"C:/Users/32831/OneDrive/Desktop/期货分析"
INTEGRATION_WT = Path(r"C:/Users/32831/wt/integration")
ACTOR = "controller-integration"


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _git_out(cwd: str, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def _prepare_worktree(master_sha: str) -> str:
    """Create or reset the integration worktree onto current master."""
    if INTEGRATION_WT.exists():
        current = _git_out(str(INTEGRATION_WT), "rev-parse", "HEAD")
        if current != master_sha:
            reset = _git(str(INTEGRATION_WT), "reset", "--hard", master_sha)
            if reset.returncode != 0:
                raise RuntimeError(f"reset integration worktree: {reset.stderr[:200]}")
            clean = _git(str(INTEGRATION_WT), "clean", "-fd")
            if clean.returncode != 0:
                raise RuntimeError(f"clean integration worktree: {clean.stderr[:200]}")
        return str(INTEGRATION_WT)
    made = _git(REPO, "worktree", "add", "--detach", str(INTEGRATION_WT), "master")
    if made.returncode != 0:
        raise RuntimeError(f"worktree add: {made.stderr[:300]}")
    return str(INTEGRATION_WT)


def _branch_of(tid: str, worktree: str) -> Optional[str]:
    """Find the branch that carries this task's commit (prefer its worktree)."""
    for suffix in ("-doubao", "-headless"):
        candidate = Path(r"C:/Users/32831/wt") / f"{tid}{suffix}"
        if (candidate / ".git").exists():
            branch = _git_out(str(candidate), "rev-parse", "--abbrev-ref", "HEAD")
            if branch and branch != "HEAD":
                return branch
    return None


def _changed_files(branch: str, master_sha: str) -> List[str]:
    out = _git_out(REPO, "diff", "--name-only", f"{master_sha}..{branch}")
    return [f for f in out.splitlines() if f.strip()]


def _local_dirty_files() -> List[str]:
    out = _git_out(REPO, "status", "--porcelain")
    files = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return files


def integrate(tasks: List[str], *, apply: bool, verify: Optional[str]) -> Dict[str, Any]:
    master_sha = _git_out(REPO, "rev-parse", "master")
    report: Dict[str, Any] = {"master_before": master_sha, "steps": [], "merged": [],
                              "conflicts": [], "skipped": []}
    branches: List[Tuple[str, str]] = []
    for tid in tasks:
        branch = _branch_of(tid, str(INTEGRATION_WT))
        if not branch:
            report["skipped"].append({"task_id": tid, "reason": "no branch found"})
            continue
        branches.append((tid, branch))

    incoming = set()
    for tid, branch in branches:
        incoming.update(_changed_files(branch, master_sha))
    dirty = set(_local_dirty_files())
    overlap = sorted(incoming & dirty)
    report["incoming_files"] = len(incoming)
    report["local_dirty_files"] = len(dirty)
    report["overlap_needs_stash"] = overlap

    if not apply:
        report["dry_run"] = True
        report["plan"] = [{"task_id": t, "branch": b} for t, b in branches]
        return report

    wt = _prepare_worktree(master_sha)
    for tid, branch in branches:
        merged = _git(wt, "merge", "--no-ff", branch, "-m", f"Merge {tid}: {branch}")
        if merged.returncode != 0:
            conflicted = _git_out(wt, "diff", "--name-only", "--diff-filter=U")
            report["conflicts"].append({"task_id": tid, "branch": branch,
                                        "files": [f for f in conflicted.splitlines() if f.strip()]})
            _git(wt, "merge", "--abort")
            report["steps"].append({"task_id": tid, "action": "merge-aborted-conflict"})
            continue
        report["merged"].append({"task_id": tid, "branch": branch,
                                 "commit": _git_out(wt, "rev-parse", "HEAD")})
        report["steps"].append({"task_id": tid, "action": "merged"})

    if not report["merged"]:
        return report

    if verify:
        test = _git(wt, "rev-parse", "HEAD")
        report["verify"] = f"merged head {test} (verify hook: {verify})"

    new_head = _git_out(wt, "rev-parse", "HEAD")
    stash_ref = ""
    if overlap:
        # Stash ONLY the overlapping files; everything else stays untouched.
        pathspec = " -- ".join(overlap)
        stashed = _git(REPO, "stash", "push", "-m",
                       "integrate: preserve local changes on overlapping files",
                       "--", *overlap)
        stash_ref = _git_out(REPO, "rev-parse", "refs/stash")
        report["steps"].append({"action": "stash-overlap", "files": overlap,
                                "ok": stashed.returncode == 0})
    ff = _git(REPO, "merge", "--ff-only", new_head)
    report["steps"].append({"action": "ff-only", "ok": ff.returncode == 0,
                            "error": ff.stderr.strip()[:200] if ff.returncode else ""})
    if ff.returncode != 0:
        if stash_ref:
            _git(REPO, "stash", "pop")
            report["steps"].append({"action": "stash-pop-restore"})
        return report
    if stash_ref:
        popped = _git(REPO, "stash", "pop")
        report["steps"].append({"action": "stash-pop-restore", "ok": popped.returncode == 0,
                                "conflict": bool(popped.stdout and "CONFLICT" in popped.stdout)})
    report["master_after"] = _git_out(REPO, "rev-parse", "master")

    for entry in report["merged"]:
        tid = entry["task_id"]
        try:
            if protocol.get_task(tid)["status"] == "ACCEPTED":
                protocol.transition(tid, "INTEGRATION", ACTOR,
                                    f"merged into master as {entry['commit'][:10]}")
                protocol.transition(tid, "DONE", ACTOR,
                                    f"integrated into master ({entry['commit'][:10]})")
                protocol.add_evidence(tid, "commit_sha", f"commit:{entry['commit']}", ACTOR,
                                      sha256=entry["commit"], sync_audit=False)
                entry["console"] = protocol.get_task(tid)["status"]
        except Exception as exc:  # noqa: BLE001
            entry["console_error"] = f"{type(exc).__name__}: {exc}"
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Integrate accepted task branches into master.")
    parser.add_argument("--tasks", required=True, help="comma separated task ids")
    parser.add_argument("--apply", action="store_true", help="actually merge (default: dry run)")
    parser.add_argument("--verify", help="optional verification note for the merged worktree")
    args = parser.parse_args(argv)
    protocol.ensure_schema()
    report = integrate([t.strip() for t in args.tasks.split(",") if t.strip()],
                       apply=args.apply, verify=args.verify)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["conflicts"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
