#!/usr/bin/env python3
"""Phase 1 MVP Protocol Layer — thin Git-backed task pool + SQLite state."""

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

LEGACY_DB_PATH = Path("/tmp/phase1_protocol.db")
ROLES_DIR = Path(__file__).parent / "roles"
PACKAGES_DIR = Path(__file__).parent / "task-packages"


def _resolve_db_path() -> Path:
    """Resolve the protocol DB path (single source of truth).

    Priority: PHASE1_DB env var > legacy /tmp path (when it exists)
    > <system temp dir>/phase1/phase1_protocol.db.
    """
    env_value = (os.environ.get("PHASE1_DB") or "").strip()
    if env_value:
        return Path(env_value)
    if LEGACY_DB_PATH.exists():
        return LEGACY_DB_PATH
    return Path(tempfile.gettempdir()) / "phase1" / "phase1_protocol.db"


DB_PATH = _resolve_db_path()

SCHEMA_VERSION = 1

# Review decisions that require an independent reviewer
# (P1_SCHEMA_DESIGN.md section 6.2): the implementer of a task may
# not accept or reject it.
REVIEW_DECISIONS = {"ACCEPTED", "REWORK"}

# Controlled executors seeded into every v1 database (fresh install and
# migration alike). Mirrors the seed table of P1_SCHEMA_DESIGN.md section 3.
_EXECUTOR_SEED = (
    ("doubao", "豆包(人工复制粘贴)", "doubao", None),
    ("codex", "Codex CLI", "codex", "OPENAI_BASE_URL"),
    ("claude", "Claude Code", "claude", "ANTHROPIC_BASE_URL"),
    ("cursor", "Cursor CLI", "cursor", None),
    ("human", "人工接管", "human", None),
)

# Tables that already exist in a v0 (pre-P1) database.
_V0_TABLES = ("role_specs", "tasks", "audit_log", "claims")

# Columns the v0 -> v1 migration adds to the existing tables. SQLite cannot
# attach a CHECK constraint to a column added with ALTER TABLE, so a migrated
# database carries no library level CHECK on these columns; only a fresh
# install (schema.sql) gets the full v1 constraints.
_V1_ADDED_COLUMNS = (
    "ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium'",
    "ALTER TABLE tasks ADD COLUMN dependencies_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE tasks ADD COLUMN executor_id TEXT",
    "ALTER TABLE tasks ADD COLUMN implementer_executor_id TEXT",
    "ALTER TABLE tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 3",
    "ALTER TABLE tasks ADD COLUMN allowed_paths_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE tasks ADD COLUMN behavioral_check TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN pass_condition TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN last_error TEXT",
    "ALTER TABLE audit_log ADD COLUMN evidence_path TEXT",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _schema_sql() -> str:
    return (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


def _is_fresh_database(conn: sqlite3.Connection) -> bool:
    """True when the database carries none of the v0 business tables."""
    placeholders = ",".join("?" * len(_V0_TABLES))
    count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        f"AND name IN ({placeholders})",
        _V0_TABLES,
    ).fetchone()[0]
    return count == 0


def _migrate_v0_to_v1(conn: sqlite3.Connection) -> None:
    """Upgrade a v0 database to v1 — additions only, nothing is dropped."""
    # The CREATE TABLE IF NOT EXISTS statements of the v1 schema are no-ops
    # for the tables that already exist, so they only add what is missing
    # (executors / task_dependencies / task_evidence) plus the indexes.
    conn.executescript(_schema_sql())
    for statement in _V1_ADDED_COLUMNS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            # Column already exists -> re-running the migration is a no-op.
            pass


def _seed_executors(conn: sqlite3.Connection) -> None:
    """Insert the controlled executors; rows that already exist are kept."""
    now = utcnow()
    conn.executemany(
        "INSERT OR IGNORE INTO executors "
        "(executor_id, label, kind, endpoint_env, active, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        [(executor_id, label, kind, endpoint_env, now)
         for executor_id, label, kind, endpoint_env in _EXECUTOR_SEED],
    )


def ensure_schema() -> None:
    """Bring the database up to SCHEMA_VERSION. Safe to call repeatedly."""
    with get_db() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        if _is_fresh_database(conn):
            conn.executescript(_schema_sql())
        else:
            _migrate_v0_to_v1(conn)
        _seed_executors(conn)
        conn.commit()
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db():
    ensure_schema()
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
        # Upsert that updates the existing row in place. INSERT OR REPLACE
        # deletes the parent row and re-inserts it, which under
        # foreign_keys=ON leaves the tasks -> role_specs reference pointing
        # at a brand new row instead of the existing one.
        conn.execute("""
            INSERT INTO role_specs (role_id, name, content_hash, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(role_id) DO UPDATE SET
                name=excluded.name,
                content_hash=excluded.content_hash,
                content=excluded.content,
                created_at=excluded.created_at
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


def atomic_claim(task_id: str, agent_id: str, executor_id: Optional[str] = None) -> dict:
    """Atomic READY → CLAIMED. Returns task package dict.

    executor_id is optional. When given it must name an active row in
    executors and is frozen into tasks.implementer_executor_id at claim
    time; when omitted the column is left NULL and the call keeps the
    legacy behaviour.
    """
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if executor_id is not None:
            executor = conn.execute(
                "SELECT active FROM executors WHERE executor_id=?", (executor_id,)
            ).fetchone()
            if executor is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Unknown executor {executor_id}")
            if not executor["active"]:
                conn.execute("ROLLBACK")
                raise ValueError(f"Executor {executor_id} is not active")

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

        cur = conn.execute("""
            UPDATE tasks SET status='CLAIMED', claimed_by=?, claimed_at=?,
                             package_hash=?, updated_at=?, implementer_executor_id=?
            WHERE task_id=? AND status='READY'
        """, (agent_id, now, package_hash, now, executor_id, task_id))
        # cur.rowcount is the number of rows this statement touched;
        # conn.total_changes counts every change on the connection and is
        # therefore only accidentally correct here.
        if cur.rowcount == 0:
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


def _self_acceptance_conflict(task: dict, actor: str,
                              actor_executor_id: Optional[str]) -> Optional[tuple[str, str]]:
    """Return (implementer, reviewer) when the reviewer is the implementer.

    The implementer identity is the executor frozen into the task at claim
    time (tasks.implementer_executor_id) or -- for a legacy claim that
    carries no executor id -- the free-text claimed_by. The reviewer
    identity is the actor_executor_id of the caller or, when it is not
    given, the free-text actor. Both are plain strings: no authentication,
    no RBAC (P1_SCHEMA_DESIGN.md section 6.2).

    Limitation, recorded honestly: this can only prove that one identity
    string tried to accept its own work. The stronger demand of the A7 role
    spec -- an independent session that does not resume the implementer
    conversation -- cannot be proven by the state machine and stays a matter
    of role discipline plus the audit trail.
    """
    implementer = task.get("implementer_executor_id") or task.get("claimed_by")
    reviewer = actor_executor_id or actor
    if implementer and reviewer and reviewer == implementer:
        return implementer, reviewer
    return None


def transition(task_id: str, to_status: str, actor: str, reason: str = "", evidence: str = "",
               evidence_path: Optional[str] = None,
               actor_executor_id: Optional[str] = None):
    valid = {
        "CLAIMED": ["IN_PROGRESS", "FAILED", "BLOCKED", "NEEDS_HUMAN"],
        "IN_PROGRESS": ["SUBMITTED", "FAILED", "BLOCKED", "NEEDS_HUMAN"],
        "SUBMITTED": ["REVIEW"],
        "REVIEW": ["ACCEPTED", "REWORK", "NEEDS_HUMAN"],
        "REWORK": ["CLAIMED", "IN_PROGRESS"],
        "ACCEPTED": ["INTEGRATION"],
        "INTEGRATION": ["REVIEW", "DONE"],
        "FAILED": ["IN_PROGRESS", "BLOCKED"],
        "BLOCKED": ["IN_PROGRESS", "NEEDS_HUMAN"],
        "NEEDS_HUMAN": ["IN_PROGRESS", "REVIEW", "BLOCKED"],
    }
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, implementer_executor_id, claimed_by FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Task {task_id} not found")
        current = row["status"]
        if to_status not in valid.get(current, []):
            raise ValueError(f"Invalid transition {current} → {to_status}")
        if current == "REVIEW" and to_status in REVIEW_DECISIONS:
            conflict = _self_acceptance_conflict(dict(row), actor, actor_executor_id)
            if conflict is not None:
                implementer, reviewer = conflict
                _audit(
                    conn, task_id, actor, current, current,
                    f"self-acceptance rejected: reviewer {reviewer} == implementer {implementer}",
                    f"attempted={to_status}",
                )
                # The "with get_db() as conn" block rolls the transaction back
                # when the exception leaves it, and the rollback would drop
                # the rejection audit as well. Commit it first so the refused
                # attempt stays visible in the audit trail.
                conn.commit()
                raise PermissionError(
                    f"self-acceptance forbidden: reviewer {reviewer} == implementer {implementer} of {task_id}"
                )
        now = utcnow()
        conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
            (to_status, now, task_id)
        )
        _audit(conn, task_id, actor, current, to_status, reason, evidence, evidence_path)
    print(f"Task {task_id}: {current} → {to_status}")


def _audit(conn, task_id, actor, from_status, to_status, reason, evidence="",
           evidence_path: Optional[str] = None):
    conn.execute("""
        INSERT INTO audit_log (task_id, actor, from_status, to_status, reason, evidence,
                               evidence_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (task_id, actor, from_status, to_status, reason, evidence, evidence_path, utcnow()))


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
