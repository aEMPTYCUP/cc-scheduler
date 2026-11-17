#!/usr/bin/env python3
"""Phase 1 MVP Protocol Layer — thin Git-backed task pool + SQLite state."""

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path("/tmp/phase1_protocol.db")
ROLES_DIR = Path(__file__).parent / "roles"
PACKAGES_DIR = Path(__file__).parent / "task-packages"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    schema = (Path(__file__).parent / "schema.sql").read_text()
    with get_db() as conn:
        conn.executescript(schema)
    print(f"DB initialized at {DB_PATH}")


def load_role_spec(role_id: str) -> dict:
    agent_md = ROLES_DIR / role_id / "AGENT.md"
    if not agent_md.exists():
        raise FileNotFoundError(f"Role spec not found: {agent_md}")
    content = agent_md.read_text(encoding="utf-8")
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    return {"role_id": role_id, "content": content, "content_hash": content_hash}


def register_role(role_id: str):
    spec = load_role_spec(role_id)
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO role_specs (role_id, name, content_hash, content, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (role_id, role_id, spec["content_hash"], spec["content"], utcnow()))
    print(f"Registered role {role_id} hash={spec['content_hash'][:12]}")


def create_task(task_id: str, title: str, description: str, role_id: str,
                acceptance_criteria: str, created_by: str = "A0") -> str:
    now = utcnow()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO tasks (task_id, title, description, role_id, status,
                               acceptance_criteria, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'BACKLOG', ?, ?, ?, ?)
        """, (task_id, title, description, role_id, acceptance_criteria, created_by, now, now))
        _audit(conn, task_id, created_by, None, "BACKLOG", "task created")
    print(f"Created task {task_id} in BACKLOG")
    return task_id


def ready_task(task_id: str, actor: str = "A0"):
    with get_db() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row or row["status"] != "BACKLOG":
            raise ValueError(f"Task {task_id} not in BACKLOG")
        now = utcnow()
        conn.execute("UPDATE tasks SET status='READY', updated_at=? WHERE task_id=?", (now, task_id))
        _audit(conn, task_id, actor, "BACKLOG", "READY", "dependencies satisfied")
    print(f"Task {task_id} → READY")


def atomic_claim(task_id: str, agent_id: str) -> dict:
    """Atomic READY → CLAIMED. Returns task package dict."""
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id=? AND status='READY'", (task_id,)
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise ValueError(f"Task {task_id} not READY or already claimed")

        role = conn.execute(
            "SELECT * FROM role_specs WHERE role_id=?", (row["role_id"],)
        ).fetchone()
        if not role:
            conn.execute("ROLLBACK")
            raise ValueError(f"Role {row['role_id']} not registered")

        package = _build_package(row, role, agent_id)
        package_hash = package["package_hash"]
        now = utcnow()

        conn.execute("""
            UPDATE tasks SET status='CLAIMED', claimed_by=?, claimed_at=?,
                             package_hash=?, updated_at=?
            WHERE task_id=? AND status='READY'
        """, (agent_id, now, package_hash, now, task_id))
        if conn.total_changes == 0:
            conn.execute("ROLLBACK")
            raise RuntimeError("Claim race lost")

        conn.execute("""
            INSERT INTO claims (task_id, agent_id, role_id, package_hash, claimed_at)
            VALUES (?, ?, ?, ?, ?)
        """, (task_id, agent_id, row["role_id"], package_hash, now))
        _audit(conn, task_id, agent_id, "READY", "CLAIMED", f"claimed by {agent_id}")
        conn.execute("COMMIT")

    _write_package(task_id, package)
    print(f"Claimed {task_id} by {agent_id}, package_hash={package_hash[:12]}")
    return package


def _build_package(task: sqlite3.Row, role: sqlite3.Row, agent_id: str) -> dict:
    role_spec = role["content"]
    role_hash = role["content_hash"]
    manifest = {
        "task_id": task["task_id"],
        "title": task["title"],
        "description": task["description"],
        "role_id": task["role_id"],
        "role_spec_sha256": role_hash,
        "acceptance_criteria": task["acceptance_criteria"],
        "claimed_by": agent_id,
        "claimed_at": utcnow(),
    }
    payload = json.dumps(manifest, sort_keys=True) + role_spec
    package_hash = hashlib.sha256(payload.encode()).hexdigest()
    manifest["package_hash"] = package_hash
    return {
        "manifest": manifest,
        "ROLE_SPEC.md": role_spec,
        "package_hash": package_hash,
    }


def _write_package(task_id: str, package: dict):
    pkg_dir = PACKAGES_DIR / task_id
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "ROLE_SPEC.md").write_text(package["ROLE_SPEC.md"], encoding="utf-8")
    (pkg_dir / "manifest.json").write_text(
        json.dumps(package["manifest"], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Package written to {pkg_dir}")


def transition(task_id: str, to_status: str, actor: str, reason: str = "", evidence: str = ""):
    valid = {
        "CLAIMED": ["IN_PROGRESS"],
        "IN_PROGRESS": ["SUBMITTED"],
        "SUBMITTED": ["REVIEW"],
        "REVIEW": ["ACCEPTED", "REWORK"],
        "REWORK": ["CLAIMED", "IN_PROGRESS"],
        "ACCEPTED": ["INTEGRATION"],
        "INTEGRATION": ["REVIEW", "DONE"],
    }
    with get_db() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError(f"Task {task_id} not found")
        current = row["status"]
        if to_status not in valid.get(current, []):
            raise ValueError(f"Invalid transition {current} → {to_status}")
        now = utcnow()
        conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
            (to_status, now, task_id)
        )
        _audit(conn, task_id, actor, current, to_status, reason, evidence)
    print(f"Task {task_id}: {current} → {to_status}")


def _audit(conn, task_id, actor, from_status, to_status, reason, evidence=""):
    conn.execute("""
        INSERT INTO audit_log (task_id, actor, from_status, to_status, reason, evidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (task_id, actor, from_status, to_status, reason, evidence, utcnow()))


def get_task(task_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(status: Optional[str] = None) -> list:
    with get_db() as conn:
        if status:
            rows = conn.execute("SELECT * FROM tasks WHERE status=? ORDER BY created_at", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


def create_worktree(repo_path: str, task_id: str, base_ref: str = "HEAD") -> str:
    """Create an isolated git worktree for a task. Returns worktree path."""
    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"Not a git repo: {repo}")
    worktree_path = repo.parent / f"wt-{task_id}"
    if worktree_path.exists():
        raise FileExistsError(f"Worktree already exists: {worktree_path}")
    branch = f"task/{task_id}"
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), base_ref],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET worktree_path=?, updated_at=? WHERE task_id=?",
            (str(worktree_path), utcnow(), task_id),
        )
    print(f"Worktree created: {worktree_path} (branch {branch})")
    return str(worktree_path)


def remove_worktree(repo_path: str, task_id: str, force: bool = False):
    """Remove a task worktree."""
    repo = Path(repo_path).resolve()
    task = get_task(task_id)
    if not task or not task.get("worktree_path"):
        print(f"No worktree recorded for {task_id}")
        return
    wt = task["worktree_path"]
    cmd = ["git", "worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(wt)
    subprocess.run(cmd, cwd=repo, check=True, capture_output=True, text=True)
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET worktree_path=NULL, updated_at=? WHERE task_id=?",
            (utcnow(), task_id),
        )
    print(f"Worktree removed: {wt}")


def list_worktrees(repo_path: str) -> list:
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip().split("\n\n") if result.stdout.strip() else []


def get_audit(task_id: str) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]
