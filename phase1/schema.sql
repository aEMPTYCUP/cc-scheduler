-- Phase 1 MVP Protocol Schema
-- SQLite + Git hybrid
-- v1 layout: phase1/P1_SCHEMA_DESIGN.md section 3.

PRAGMA journal_mode=DELETE;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS role_specs (
    role_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 新增：受控执行器注册表
CREATE TABLE IF NOT EXISTS executors (
    executor_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('doubao','codex','claude','cursor','human')),
    endpoint_env TEXT,                 -- 如 ANTHROPIC_BASE_URL / OPENAI_BASE_URL；人工为 NULL
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    role_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'BACKLOG',
    acceptance_criteria TEXT,         -- 保留：人类可读验收描述（旧字段不动）
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    claimed_by TEXT,                  -- 保留：自由文本展示名，不删除、不改语义
    claimed_at TEXT,
    package_hash TEXT,
    worktree_path TEXT,
    -- ↓↓↓ P1 新增列 ↓↓↓
    priority TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('high','medium','low')),
    dependencies_json TEXT NOT NULL DEFAULT '[]',   -- 冗余快照；权威关系见 task_dependencies
    executor_id TEXT REFERENCES executors(executor_id),
    implementer_executor_id TEXT,     -- 领取时固化的实现者身份，自验收守卫依据
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,        -- 对齐老引擎 3 次上限
    allowed_paths_json TEXT NOT NULL DEFAULT '[]', -- 结构化契约
    behavioral_check TEXT NOT NULL DEFAULT '',
    pass_condition TEXT NOT NULL DEFAULT '',
    last_error TEXT,                  -- 便于看板直接显示阻塞原因
    FOREIGN KEY (role_id) REFERENCES role_specs(role_id)
);

-- 新增：依赖关系（规范化，便于"依赖全部 DONE 才可领取"查询）
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    depends_on TEXT NOT NULL REFERENCES tasks(task_id),
    PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    evidence TEXT,
    evidence_path TEXT,               -- 新增：证据文件路径（日志/diff/SHA）
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

-- 新增：证据索引（支持一个任务多份证据，替代把信息塞进单个文本列）
CREATE TABLE IF NOT EXISTS task_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    kind TEXT NOT NULL CHECK (kind IN ('cli_log','diff_patch','commit_sha','package_hash','other')),
    path TEXT NOT NULL,
    sha256 TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    package_hash TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    released_at TEXT,                 -- 已存在但无人写；P1 由 release_claim 启用
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_log(task_id);
CREATE INDEX IF NOT EXISTS idx_deps_task ON task_dependencies(task_id);
CREATE INDEX IF NOT EXISTS idx_evidence_task ON task_evidence(task_id);
