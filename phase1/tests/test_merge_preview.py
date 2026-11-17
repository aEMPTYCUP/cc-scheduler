#!/usr/bin/env python3
r"""Read-only merge-preview tests on throwaway git repositories.

analyze_merge / GitIntegrationAdapter.preview must never move a ref, touch the
index or change the working tree. These build ephemeral repos in tmp_path, so
the production repository is never involved.
"""

from __future__ import annotations

import subprocess

import pytest

import orchestrator


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _repo(path, branch="master"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "base")
    return path


def _head(repo, branch="master"):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", branch],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_preview_fast_forward(tmp_path):
    repo = _repo(tmp_path / "r")
    _git(repo, "checkout", "-b", "feature")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "feat")

    result = orchestrator.analyze_merge(repo, "feature", "master")
    assert result["conflicts"] is False
    assert result["up_to_date"] is True
    assert result["fast_forwardable"] is True
    assert result["needs_rebase"] is False
    assert result["verdict"] == "fast_forward"
    assert "new.txt" in result["changed_files"]
    assert result["writable"] is False


def test_preview_diverged_clean_three_way(tmp_path):
    repo = _repo(tmp_path / "r")
    _git(repo, "checkout", "-b", "feature")
    (repo / "g.txt").write_text("feature-only\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "feature file")
    _git(repo, "checkout", "master")
    (repo / "h.txt").write_text("master-only\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "master file")

    result = orchestrator.analyze_merge(repo, "feature", "master")
    assert result["conflicts"] is False
    assert result["up_to_date"] is False
    assert result["needs_rebase"] is True
    assert result["fast_forwardable"] is False
    assert result["verdict"] == "merge_needed"
    assert "g.txt" in result["changed_files"]


def test_preview_detects_conflict_and_files(tmp_path):
    repo = _repo(tmp_path / "r")
    _git(repo, "checkout", "-b", "feature")
    (repo / "f.txt").write_text("base\nfeature\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "feature edit")
    _git(repo, "checkout", "master")
    (repo / "f.txt").write_text("base\nmaster\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "master edit")

    result = orchestrator.analyze_merge(repo, "feature", "master")
    assert result["conflicts"] is True
    assert result["verdict"] == "conflict"
    assert "f.txt" in result["conflict_files"]
    assert result["fast_forwardable"] is False


def test_preview_unknown_branch_raises(tmp_path):
    repo = _repo(tmp_path / "r")
    with pytest.raises(ValueError, match="unknown branch"):
        orchestrator.analyze_merge(repo, "does-not-exist", "master")


def test_preview_is_strictly_read_only(tmp_path):
    repo = _repo(tmp_path / "r")
    _git(repo, "checkout", "-b", "feature")
    (repo / "f.txt").write_text("base\nfeature\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "edit")
    _git(repo, "checkout", "master")
    (repo / "f.txt").write_text("base\nmaster\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "master edit")

    master_before = _head(repo)
    feature_before = _head(repo, "feature")
    # Conflict case is the most invasive-looking analysis.
    orchestrator.analyze_merge(repo, "feature", "master")
    # Nothing moved: refs identical, working tree still clean.
    assert _head(repo) == master_before
    assert _head(repo, "feature") == feature_before
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status == ""
    assert subprocess.run(["git", "-C", str(repo), "branch", "--show-current"],
                          capture_output=True, text=True, check=True).stdout.strip() == "master"


def test_adapter_preview_resolves_branch_from_worktree(tmp_path):
    repo = _repo(tmp_path / "r")
    # A linked worktree on a task branch, mirroring the real wt/ layout: the
    # task branch lives in the worktree while the main checkout stays on master.
    _git(repo, "checkout", "-b", "doubao/t-1-auto")
    (repo / "task.txt").write_text("done\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "task work")
    _git(repo, "checkout", "master")
    worktree = tmp_path / "wt-task"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", str(worktree),
                    "doubao/t-1-auto"], check=True, capture_output=True)

    adapter = orchestrator.GitIntegrationAdapter(str(repo), target_branch="master")
    result = adapter.preview(task={"worktree_path": str(worktree)})
    assert result["source_branch"] == "doubao/t-1-auto"
    assert result["verdict"] == "fast_forward"
