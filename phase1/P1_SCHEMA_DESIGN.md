# P1 Schema 与状态机增量设计（可评审 / 待实现）

> 状态：设计稿。本文件给出 P1 要落地的具体表结构、迁移、状态机与函数改动，**当前未改任何源码**。
> 上层背景见 [PHASE_2_REPLACEMENT.md](PHASE_2_REPLACEMENT.md)。
> 实现铁律：只增量、不改旧义；所有状态变更仍只走 `protocol.transition` 并写 audit_log。

## 1. 范围与兼容性承诺

P1 只做协议补平，不接真实 CLI、不做编排循环、不割接。交付后必须满足：

- `cli.py`、`claude_adapter.py`、`cursor_adapter.py`、`assisted_pilot.py`、`mark_submitted.py`、`ui_server.py`、`export_for_ui.py` **不改或仅因 import 路径微调即可继续运行**。
- 现有 8 个协议函数的调用方式不变；新能力通过新增函数与"带默认值的新关键字参数"提供。
- 旧数据库（v0）启动时自动迁移到 v1，既有 tasks/audit 数据保留。

## 2. DB 路径与迁移机制

### 2.1 路径单一来源（修复两处硬编码）

现状：`protocol.py:13` 与 `export_for_ui.py:24` 各自硬编码 `Path("/tmp/phase1_protocol.db")`，本机实测解析为 `C:\tmp\...`，且不在仓库内。改为 protocol.py 内唯一解析，其它模块 import：

```python
# protocol.py
import os, tempfile
def _resolve_db_path() -> Path:
    env = os.environ.get("PHASE1_DB")
    if env:
        return Path(env)
    legacy = Path("/tmp/phase1_protocol.db")
    if legacy.exists():          # 兼容已存在的旧库，避免静默换库丢数据
        return legacy
    base = Path(os.environ.get("TEMP") or tempfile.gettempdir()) / "phase1"
    return base / "phase1_protocol.db"

DB_PATH = _resolve_db_path()
```

`export_for_ui.py` 改为 `from protocol import DB_PATH as DEFAULT_DB`（ui_server 已把 phase1 目录加入 sys.path）。

### 2.2 用 PRAGMA user_version 做增量迁移

schema.sql 现为纯 `CREATE TABLE IF NOT EXISTS`，对已存在的库不会加列。引入版本号 + 幂等迁移：

```python
SCHEMA_VERSION = 1

def ensure_schema():
    with get_db() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            _migrate_v0_to_v1(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
```

- 新库：`init_db()` 执行完整 v1 schema（第 3 节），直接置 user_version=1。
- 旧库：跑第 4 节的 ALTER/建表迁移。
- 长驻进程（ui_server）启动时调一次 `ensure_schema()`；短命令在 `init_db()` 内调。

## 3. 完整 v1 schema（fresh install）

```sql
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
```

种子执行器（migrate 与 init 都执行，`INSERT OR IGNORE`）：

```sql
INSERT OR IGNORE INTO executors (executor_id, label, kind, endpoint_env, active, created_at) VALUES
  ('doubao',  '豆包(人工复制粘贴)', 'doubao', NULL, 1, :now),
  ('codex',   'Codex CLI',          'codex',  'OPENAI_BASE_URL', 1, :now),
  ('claude',  'Claude Code',        'claude', 'ANTHROPIC_BASE_URL', 1, :now),
  ('cursor',  'Cursor CLI',         'cursor', NULL, 1, :now),
  ('human',   '人工接管',            'human',  NULL, 1, :now);
```

## 4. v0 → v1 迁移（只增不删）

SQLite 的 `ALTER TABLE ADD COLUMN` 不支持附带 CHECK/外键，因此迁移库的新列**没有库级 CHECK**，相关合法性在应用层校验；只有第 3 节的 fresh schema 带完整 CHECK。这是已知取舍，需在 README 注明。

```sql
ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium';
ALTER TABLE tasks ADD COLUMN dependencies_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN executor_id TEXT;
ALTER TABLE tasks ADD COLUMN implementer_executor_id TEXT;
ALTER TABLE tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 3;
ALTER TABLE tasks ADD COLUMN allowed_paths_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN behavioral_check TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN pass_condition TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN last_error TEXT;
ALTER TABLE audit_log ADD COLUMN evidence_path TEXT;
-- executors / task_dependencies / task_evidence 用第 3 节的 IF NOT EXISTS 建表
-- 然后执行 executors 种子插入
```

迁移用 Python 逐句包裹 `try/except sqlite3.OperationalError`（列已存在则跳过），保证重复执行幂等。

## 5. 状态机增量（只加边，不动旧边）

在现有 `transition` 的 valid 映射基础上**追加**，原有键值原样保留：

```python
valid = {
    # —— 以下为既有边，原样保留 ——
    "CLAIMED":     ["IN_PROGRESS"],                 # 追加后见下
    "IN_PROGRESS": ["SUBMITTED"],
    "SUBMITTED":   ["REVIEW"],
    "REVIEW":      ["ACCEPTED", "REWORK"],
    "REWORK":      ["CLAIMED", "IN_PROGRESS"],
    "ACCEPTED":    ["INTEGRATION"],
    "INTEGRATION": ["REVIEW", "DONE"],
}

# P1 追加后的最终形态（旧边一条不删）：
valid_p1 = {
    "CLAIMED":     ["IN_PROGRESS", "FAILED", "BLOCKED", "NEEDS_HUMAN"],
    "IN_PROGRESS": ["SUBMITTED", "FAILED", "BLOCKED", "NEEDS_HUMAN"],
    "SUBMITTED":   ["REVIEW"],
    "REVIEW":      ["ACCEPTED", "REWORK", "NEEDS_HUMAN"],
    "REWORK":      ["CLAIMED", "IN_PROGRESS"],
    "ACCEPTED":    ["INTEGRATION"],
    "INTEGRATION": ["REVIEW", "DONE"],
    "FAILED":      ["IN_PROGRESS", "BLOCKED"],
    "BLOCKED":     ["IN_PROGRESS", "NEEDS_HUMAN"],
    "NEEDS_HUMAN": ["IN_PROGRESS", "REVIEW", "BLOCKED"],
}
```

三态语义（对齐老引擎）：

- **FAILED**：单次执行尝试失败、可重试。`record_failure` 时 retry_count+1 并写审计；未超 max_retries 落 FAILED，达上限落 BLOCKED。
- **NEEDS_HUMAN**：验收 UNCERTAIN、执行器不可用、需人工补证据。人工处理后回 IN_PROGRESS 或直接 REVIEW。
- **BLOCKED**：重试耗尽或不可自动解决，需 A0/owner；只有显式特权动作能重开（BLOCKED→IN_PROGRESS）。
- 配额冷却**不产生任务状态**，任务停在 READY/CLAIMED，仅派发层暂停（避免把"暂时不能跑"误记成失败）。

## 6. 执行器身份与自验收守卫

### 6.1 身份

- `atomic_claim(task_id, agent_id, executor_id=None)`：新增可选 executor_id；提供时校验存在于 executors 且 active=1，并把该值固化到 `tasks.implementer_executor_id`；不提供时维持旧行为（仅写 claimed_by），保证旧调用不破坏。
- REWORK 后重新领取会刷新 implementer_executor_id，留下对应审计。

### 6.2 最小权限规则（明确、可测试，不上复杂权限系统）

```python
REVIEW_DECISIONS = {"ACCEPTED", "REWORK"}

def _assert_independent_actor(task: dict, actor: str, actor_executor_id: str | None):
    implementer = task.get("implementer_executor_id") or task.get("claimed_by")
    reviewer = actor_executor_id or actor
    if implementer and reviewer and reviewer == implementer:
        raise PermissionError(
            f"self-acceptance forbidden: reviewer {reviewer} == implementer of {task['task_id']}"
        )
```

在 `transition()` 内，当 `to_status in REVIEW_DECISIONS` 且当前状态为 REVIEW 时调用；违规抛 `PermissionError`，不写状态、只写一条拒绝审计（to_status 记为原状态或专门的拒绝事件）。

**如实记录的局限**：这只能阻止"同一身份字符串自验收"。A7 AGENT.md 要求的"独立新会话、不得 resume 实现者对话"无法由状态机机械证明，仍靠角色规范与审计声明；未来接入真实认证后才能进一步收紧。不得在文档里夸大为"已强制独立会话"。

## 7. 结构化验收契约

老引擎契约三字段（scheduler-prompt.md:34-43）落到 tasks 新列：

- `allowed_paths_json`：JSON 数组，越界改文件由 acceptance-auditor 的 C-01 检查。
- `behavioral_check`：要执行的命令字符串。
- `pass_condition`：判定条件。
- 旧 `acceptance_criteria` 保留为散文描述，两者并存，不互相替代。

`create_task` 新增对应可选参数，默认空值，旧调用签名完全兼容。

## 8. 函数级改动清单（全部 additive）

| 函数 | 改动 | 兼容性 |
| --- | --- | --- |
| `_resolve_db_path / ensure_schema` | 新增 | 新 |
| `create_task` | 新增 kwargs：`priority="medium", dependencies=None, executor_id=None, allowed_paths=None, behavioral_check="", pass_condition="", max_retries=3` | 旧调用默认值等价 |
| `atomic_claim` | 新增 `executor_id=None`；写 implementer；修 rowcount（见 9.1） | 旧调用不变 |
| `transition` | valid 扩为 P1 版；接入自验收守卫；新增 `evidence_path=None` 透传 | 旧合法转换全部保留 |
| `register_role` | 改 UPSERT（见 9.2） | 行为等价、消除 FK 隐患 |
| `select_next_task` | 新增：返回依赖全 DONE 的 READY 任务，按 priority→created_at 排序，**不改状态**（领取仍归 atomic_claim） | 新 |
| `record_failure` | 新增：retry_count++、写审计、按上限落 FAILED/BLOCKED、记 last_error | 新 |
| `block_task / request_human / release_claim` | 新增：BLOCKED / NEEDS_HUMAN / 释放卡死 claim（写 claims.released_at） | 新 |
| `add_evidence` | 新增：写 task_evidence 并可同步 audit_log.evidence_path | 新 |
| `register_executor / list_executors` | 新增：执行器注册表维护 | 新 |
| `get_task / list_tasks / get_audit / create_worktree / ready_task` | 不改签名；list_tasks 可顺带返回新列 | 完全兼容 |

## 9. 两个既有 bug 的修法（P1 一并修）

### 9.1 `total_changes` 误用（protocol.py:108）

`conn.total_changes` 是连接累计变更数，不是上一条 UPDATE 的影响行数；现在仅因领取是该连接首条写语句才碰巧成立。改为使用 cursor.rowcount：

```python
cur = conn.execute(
    "UPDATE tasks SET status='CLAIMED', ... WHERE task_id=? AND status='READY'", (...)
)
if cur.rowcount == 0:
    conn.execute("ROLLBACK")
    raise RuntimeError("Claim race lost")
```

### 9.2 register_role 的外键隐患（protocol.py:49）

`INSERT OR REPLACE` 在已有 tasks 引用该 role 时会先删父行再插入，`foreign_keys=ON` 下可能触发约束失败，而 init/pilot/assisted_pilot 每次都调 register_role。改为不删除行的 UPSERT：

```sql
INSERT INTO role_specs (role_id, name, content_hash, content, created_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(role_id) DO UPDATE SET
  name=excluded.name,
  content_hash=excluded.content_hash,
  content=excluded.content,
  created_at=excluded.created_at;
```

## 10. 测试计划（pytest，全部用 tmp_path，禁止 /tmp）

通过 monkeypatch `protocol.DB_PATH` 指向 `tmp_path` 下的临时库。最小用例集：

1. 状态机：每条合法边可转换；非法边抛 ValueError；既有 happy path 仍能从 BACKLOG 走到 DONE。
2. 异常态：record_failure 未超限时落 FAILED 且 retry_count 递增；达到 max_retries 落 BLOCKED；NEEDS_HUMAN 来回转换合法。
3. 领取竞态：两个连接并发 atomic_claim 同一 READY 任务，恰一个成功，另一个抛错（验证 9.1 修复）。
4. 角色 UPSERT：先建任务引用某 role，再重复 register_role，不抛外键错误且哈希更新（验证 9.2）。
5. 自验收守卫：实现者本人调 ACCEPTED/REWORK 抛 PermissionError 且状态不变；不同 actor 可正常验收。
6. 依赖门禁：select_next_task 在依赖未 DONE 时不返回该任务；依赖全部 DONE 后按优先级返回。
7. 迁移兼容：手工建 v0 旧 schema 并插入旧数据 → ensure_schema → 新列/新表存在、旧行数与审计不变、user_version=1；重复 ensure_schema 幂等。
8. 路径解析：设 PHASE1_DB 时优先；存在 legacy `/tmp` 库时回退；否则落到平台临时目录。

CI（protocol-ci.yml）同步改造：移除 `cli.py init || true` 的吞错与只 echo 的步骤，改为 `python -m pytest phase1/tests -q`，测试失败必须让工作流变红。

## 11. P1 验收标准

- 上述 8 组测试全绿，CI 真实反映结果（无 `|| true`）。
- v0 库自动迁移到 v1 且数据无损；旧脚本与适配器不改逻辑可运行。
- 老引擎的优先级、依赖、重试上限、blocked/needs_human、实现者身份与自验收禁止，在 phase1 都有对等且可测试的承载。
- 所有新增状态变更均经 transition 并落 audit_log；无任何绕过协议层的直写 UPDATE（worktree 现有直写在 P1 顺带改为走审计，可作为本批可选清理项）。

## 12. P1 明确不做

- 不实现 codex/claude 真实调用、日志落盘、配额钩子（P2）。
- 不做 orchestrator 循环与 launcher（P3）。
- 不做 queue.json 数据迁移与任何割接（P4）。
- 不引入认证/复杂 RBAC；自验收仅字符串级守卫并如实标注局限。
- 不 push、不修改期货分析仓库与其任务计划。
