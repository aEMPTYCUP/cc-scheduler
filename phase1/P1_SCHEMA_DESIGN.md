# P1 Schema 与状态机增量设计（已实现）

> 状态：**已实现**。P1 编码任务 T-01 ~ T-09 已于 2026-11-17 全部完成并通过独立验收，本文件既是设计稿，也是当前实现的权威描述：各节已标注「已实现（T-0x）」；实现与设计不一致的少数几处，已在原地按代码更正并写明差异。
> 上层背景见 [PHASE_2_REPLACEMENT.md](PHASE_2_REPLACEMENT.md)。
> 实现铁律：只增量、不改旧义；所有状态变更仍只走 `protocol.transition` 并写 audit_log。
> 上层背景见 [PHASE_2_REPLACEMENT.md](PHASE_2_REPLACEMENT.md)。
> 实现铁律：只增量、不改旧义；所有状态变更仍只走 `protocol.transition` 并写 audit_log。

## 1. 范围与兼容性承诺（已实现）

P1 只做协议补平，不接真实 CLI、不做编排循环、不割接。交付后必须满足：

- `cli.py`、`claude_adapter.py`、`cursor_adapter.py`、`assisted_pilot.py`、`mark_submitted.py`、`ui_server.py`、`export_for_ui.py` **不改或仅因 import 路径微调即可继续运行**。
- 现有 8 个协议函数的调用方式不变；新能力通过新增函数与"带默认值的新关键字参数"提供。
- 旧数据库（v0）启动时自动迁移到 v1，既有 tasks/audit 数据保留。
实现结果：`cli.py`、各适配器、看板脚本、`assisted_pilot.py`、`mark_submitted.py` 未改逻辑即可运行；86 条测试的覆盖见第 10 节。


## 2. DB 路径与迁移机制（已实现：T-01 / T-04）

### 2.1 路径单一来源（修复两处硬编码）——已实现（T-01）

现状（设计时）：`protocol.py:13` 与 `export_for_ui.py:24` 各自硬编码 `Path("/tmp/phase1_protocol.db")`，本机实测解析为 `C:\tmp\...`，且不在仓库内。改为 protocol.py 内唯一解析，其它模块 import。**已实现（T-01）**，实际代码：

```python
# protocol.py
LEGACY_DB_PATH = Path("/tmp/phase1_protocol.db")

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
```

与设计稿的三处偏差（以代码为准）：

- 环境变量先 `.strip()`：只有空格的 `PHASE1_DB` 视为未设置（见 `phase1/tests/test_db_path.py::test_blank_env_var_is_ignored`）。
- 临时目录用 `tempfile.gettempdir()`，不直接读 `TEMP`；Windows 上两者等价，但 `tempfile` 也能覆盖没设 `TEMP` 的环境。
- `LEGACY_DB_PATH` 提为模块常量，测试可以 monkeypatch 它，而不是去碰文件系统上的真实路径。

`export_for_ui.py` 改为 `from protocol import DB_PATH as DEFAULT_DB`（ui_server 已把 phase1 目录加入 sys.path）。`get_db()` 在连接前会 `DB_PATH.parent.mkdir(parents=True, exist_ok=True)`，所以 `PHASE1_DB` 可以指向尚不存在的路径，父目录自动建出来。

### 2.2 用 PRAGMA user_version 做增量迁移——已实现（T-04）

schema.sql 现为纯 `CREATE TABLE IF NOT EXISTS`，对已存在的库不会加列。引入版本号 + 幂等迁移。**已实现（T-04）**，实际代码：

```python
SCHEMA_VERSION = 1

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
```

与设计稿的两处偏差（以代码为准）：

- 设计稿只写了 `version < 1 → _migrate_v0_to_v1`；实现先用 `_is_fresh_database()` 判断库里有没有 v0 的四张业务表（`role_specs` / `tasks` / `audit_log` / `claims`），全新库直接跑完整 v1 schema，旧库才走增量迁移。
- `PRAGMA user_version` 在 `conn.commit()` **之后**设置；执行器种子 `_seed_executors()`（`INSERT OR IGNORE`）在两条分支之后都会跑，所以新库和迁移库都有同样五个执行器。

- 新库：`init_db()` 执行完整 v1 schema（第 3 节），直接置 user_version=1。
- 旧库：跑第 4 节的 ALTER/建表迁移。
- 长驻进程（ui_server）启动时调一次 `ensure_schema()`；短命令在 `init_db()` 内调。

## 3. 完整 v1 schema（fresh install）——已实现（T-04）

与 `phase1/schema.sql` 逐条对应。种子执行器见本节末尾的 INSERT —— 代码里是 `protocol._EXECUTOR_SEED` 常量加 `_seed_executors()`（`INSERT OR IGNORE`），fresh install 与迁移 alike。

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

## 4. v0 → v1 迁移（只增不删）——已实现（T-04）

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

实现（已实现，T-04）：上面的 ADD COLUMN 列表就是 `protocol._V1_ADDED_COLUMNS`，逐句执行、重复执行幂等；迁移后 `PRAGMA user_version = 1`，`PRAGMA foreign_key_check` 为空，旧行数与审计行数不变。

迁移库的新列**没有库级 CHECK**，合法性在应用层兜底：`create_task()` 校验 `priority` 与 `max_retries`，在迁移库上同样生效（见 `phase1/tests/test_selection.py::test_migrated_database_still_rejects_an_invalid_priority`）。

## 5. 状态机增量（只加边，不动旧边）——已实现（T-05）

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
- 配额冷却**不产生任务状态**，任务停在 READY/CLAIMED，仅派发层暂停（避免把“暂时不能跑”误记成失败）。

实现（已实现，T-05）：`protocol.transition()` 里的 `valid` 映射与上面的 `valid_p1` **逐条一致**，新增 14 条合法边，原有 10 条边一条未改、顺序未变；`transition` 同时新增 `evidence_path=None` 关键字参数，透传写入 `audit_log.evidence_path`。

三态在协议层的入口：`FAILED` 由 `record_failure()` 写入；`BLOCKED` 由 `block_task()` 或 `record_failure()` 达到重试上限时写入；`NEEDS_HUMAN` 由 `request_human()` 写入。三者都经 `transition()`，因此都落审计。

## 6. 执行器身份与自验收守卫（已实现：T-06 / T-07）

### 6.1 身份——已实现（T-06 / T-07）

- `atomic_claim(task_id, agent_id, executor_id=None)`：新增可选 executor_id；提供时校验存在于 executors 且 active=1，并把该值固化到 `tasks.implementer_executor_id`；不提供时维持旧行为（仅写 claimed_by），保证旧调用不破坏。
- REWORK 后重新领取会刷新 implementer_executor_id，留下对应审计。

实现细节：`executor_id` 不存在或 `active=0` 时，`atomic_claim` 抛 `ValueError`（`Unknown executor ...` / `Executor ... is not active`）并回滚；不传该参数时 `implementer_executor_id` 保持 `NULL`，旧行为不变。

### 6.2 最小权限规则（明确、可测试，不上复杂权限系统）——已实现（T-07）

```python
REVIEW_DECISIONS = {"ACCEPTED", "REWORK"}

def _self_acceptance_conflict(task: dict, actor: str,
                              actor_executor_id: Optional[str]) -> Optional[tuple[str, str]]:
    """Return (implementer, reviewer) when the reviewer is the implementer."""
    implementer = task.get("implementer_executor_id") or task.get("claimed_by")
    reviewer = actor_executor_id or actor
    if implementer and reviewer and reviewer == implementer:
        return implementer, reviewer
    return None
```

> 设计稿里这个函数叫 `_assert_independent_actor` 且直接抛错；实现改成 `_self_acceptance_conflict`，只**判定**不抛错，由 `transition()` 决定怎么记审计、什么时候抛。

在 `transition()` 内，当 `to_status in REVIEW_DECISIONS` 且当前状态为 REVIEW 时调用。判定为冲突时：

1. 任务状态**不变**（仍停在 REVIEW）；
2. 写一条**自环审计**：`from_status = to_status = REVIEW`，`reason` 里带 `self-acceptance rejected`，`evidence` 记 `attempted=ACCEPTED` / `attempted=REWORK`；
3. **先 `conn.commit()` 把这条拒绝审计落盘，再抛 `PermissionError`** —— `with get_db() as conn` 在异常离开时会把事务回滚，直接抛会把拒绝记录一起抹掉。

抛出的异常形如 `self-acceptance forbidden: reviewer <reviewer> == implementer <implementer> of <task_id>`。

守卫只覆盖 REVIEW 态的验收决定：`REVIEW → NEEDS_HUMAN` 等非验收转换不受影响，实现者本人也可以触发。

**如实记录的局限**：这只能阻止"同一身份字符串自验收"。A7 AGENT.md 要求的"独立新会话、不得 resume 实现者对话"无法由状态机机械证明，仍靠角色规范与审计声明；未来接入真实认证后才能进一步收紧。不得在文档里夸大为"已强制独立会话"。

## 7. 结构化验收契约——已实现（T-08）

老引擎契约三字段（scheduler-prompt.md:34-43）落到 tasks 新列：

- `allowed_paths_json`：JSON 数组，越界改文件由 acceptance-auditor 的 C-01 检查。
- `behavioral_check`：要执行的命令字符串。
- `pass_condition`：判定条件。
- 旧 `acceptance_criteria` 保留为散文描述，两者并存，不互相替代。

`create_task` 新增对应可选参数，默认空值，旧调用签名完全兼容（已实现，T-08）。

### 7.1 依赖语义（`dependencies`）

`create_task(..., dependencies=[...])` 的依赖**必须在创建时已经存在**：

- 实现先查 `tasks` 表，被依赖的 task_id 只要有一个不存在就抛 `ValueError`，并把缺失的 id 一次列出来（`Unknown dependency task(s): ...`）。
- 因此**不支持前向引用**（不能先建下游再补上游），也**不支持自依赖**（任务在 INSERT 之前还不存在）。要表达依赖顺序，先建上游任务，再建下游任务并把上游 id 填进 `dependencies`。
- 依赖关系写进 `task_dependencies`（权威关系，主键 `(task_id, depends_on)`），同时写一份去重排序后的快照 `tasks.dependencies_json` 供看板直接读。
- `select_next_task()` 的门禁条件是：**所有依赖都处于 `DONE`** 才算合格；没有依赖的 READY 任务直接合格。排序为 priority（high → medium → low）→ `created_at` → `task_id`，且**只读**，不改状态、不写审计。

## 8. 函数级改动清单（全部 additive）——已实现，差异见备注

| 函数 | 改动 | 兼容性 |
| --- | --- | --- |
| `_resolve_db_path / ensure_schema / init_db` | 新增（`init_db` 调 `ensure_schema` 并打印实际库路径） | 新 |
| `create_task` | 新增 kwargs：`priority="medium", dependencies=None, allowed_paths=None, behavioral_check="", pass_condition="", max_retries=3` | 旧调用默认值等价 |
| `atomic_claim` | 新增 `executor_id=None`；写 implementer；修 rowcount（见 9.1） | 旧调用不变 |
| `transition` | valid 扩为 P1 版；接入自验收守卫；新增 `evidence_path=None` 与 `actor_executor_id=None` 透传 | 旧合法转换全部保留 |
| `register_role` | 改 UPSERT（见 9.2） | 行为等价、消除 FK 隐患 |
| `select_next_task` | 新增：返回依赖全 DONE 的 READY 任务，按 priority → created_at → task_id 排序，**不改状态**（领取仍归 atomic_claim） | 新 |
| `record_failure` | 新增：retry_count++、写审计、按上限落 FAILED/BLOCKED、记 last_error | 新 |
| `block_task / request_human / release_claim` | 新增：BLOCKED / NEEDS_HUMAN / 释放卡死 claim（写 claims.released_at） | 新 |
| `add_evidence` | 新增：写 task_evidence 并可同步 audit_log.evidence_path | 新 |
| `get_task / list_tasks / get_audit / create_worktree / ready_task` | 不改签名；`list_tasks` 直接返回新列 | 完全兼容 |

与设计稿的两处差异（以代码为准）：

- **`create_task` 没有 `executor_id` 参数。** 设计稿曾列过它，实现没有加：`tasks.executor_id` 列在 v1 schema 和迁移列里都存在，但目前没有任何函数写它；执行器身份实际固化在 `tasks.implementer_executor_id`（由 `atomic_claim(executor_id=...)` 写入）。
- **`register_executor` / `list_executors` 没有实现。** 执行器注册表目前只有种子数据（`_seed_executors()` 用 `INSERT OR IGNORE` 写入 doubao / codex / claude / cursor / human），没有对外的注册与查询 API。需要动态增删执行器时归到 P2。

## 9. 两个既有 bug 的修法（P1 一并修）——已实现（T-06）

### 9.1 `total_changes` 误用（protocol.py:108）——已实现（T-06）

`conn.total_changes` 是连接累计变更数，不是上一条 UPDATE 的影响行数；现在仅因领取是该连接首条写语句才碰巧成立。改为使用 cursor.rowcount：

```python
cur = conn.execute(
    "UPDATE tasks SET status='CLAIMED', ... WHERE task_id=? AND status='READY'", (...)
)
if cur.rowcount == 0:
    conn.execute("ROLLBACK")
    raise RuntimeError("Claim race lost")
```

实现（已实现，T-06）：`atomic_claim` 现在用 `cur.rowcount` 判定竞态。另外实测记录一条：本机 15 轮 × 8 线程（120 次领取）压测里，**败者全部在前面的 SELECT 守卫处抛 `ValueError`**，`RuntimeError("Claim race lost")` 分支实际不可达，但保留作防御性兜底；压测中无 `database is-locked`、无 flaky。

### 9.2 register_role 的外键隐患（protocol.py:49）——已实现（T-06）

`INSERT OR REPLACE` 的语义是「先删掉冲突行，再插入新行」。当 `role_specs` 已被 `tasks` 引用时，这一步是 delete + re-insert，不是原地更新。

**实测更正（2026-11-17 在本机复现，结论以实测为准）**：在当前 `NO ACTION` 外键下，`INSERT OR REPLACE` **并不会立即抛约束失败** —— 语句执行结束时新行已经存在，`PRAGMA foreign_key_check` 为空，引用它的 `tasks` 行照样能 join 上。真实危害是另外两条：

- **rowid 漂移**：父行被删后重插，rowid 从 1 变成 2。引用行的外键值没变，但父行的物理标识已经换人。
- **未来的级联删除风险**：一旦给 `tasks.role_id` 加上 `ON DELETE CASCADE`，同一句 `INSERT OR REPLACE` 会**静默删掉所有引用该 role 的任务**且不报错（实测：CASCADE 变体下引用行直接归零，语句照样成功）。

所以这次修复的价值不是「避免当场抛错」，而是**用原地更新消除这两条隐患**：

```sql
INSERT INTO role_specs (role_id, name, content_hash, content, created_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(role_id) DO UPDATE SET
  name=excluded.name,
  content_hash=excluded.content_hash,
  content=excluded.content,
  created_at=excluded.created_at;
```

`register_role` 现在每次被调用（`cli.py init`、`pilot.py`、`assisted_pilot.py` 都会调）都是一次纯 UPDATE，父行 rowid 不变，引用它的任务不受影响。

## 10. 测试计划与实测结果（pytest，全部用 tmp_path，禁止 /tmp）——已实现（T-03）

通过 monkeypatch `protocol.DB_PATH` 指向 `tmp_path` 下的临时库。**已实现（T-03）**，设计上的 8 组用例与实际落地一一对应：

1. 状态机：每条合法边可转换；非法边抛 ValueError；既有 happy path 仍能从 BACKLOG 走到 DONE。
2. 异常态：record_failure 未超限时落 FAILED 且 retry_count 递增；达到 max_retries 落 BLOCKED；NEEDS_HUMAN 来回转换合法。
3. 领取竞态：两个连接并发 atomic_claim 同一 READY 任务，恰一个成功，另一个抛错（验证 9.1 修复）。
4. 角色 UPSERT：先建任务引用某 role，再重复 register_role，不抛外键错误且哈希更新（验证 9.2）。
5. 自验收守卫：实现者本人调 ACCEPTED/REWORK 抛 PermissionError 且状态不变；不同 actor 可正常验收。
6. 依赖门禁：select_next_task 在依赖未 DONE 时不返回该任务；依赖全部 DONE 后按优先级返回。
7. 迁移兼容：手工建 v0 旧 schema 并插入旧数据 → ensure_schema → 新列/新表存在、旧行数与审计不变、user_version=1；重复 ensure_schema 幂等。
8. 路径解析：设 PHASE1_DB 时优先；存在 legacy `/tmp` 库时回退；否则落到平台临时目录。

实际落地为 8 个测试文件、**86 条用例**（2026-11-17 实测 `86 passed`，退出码 0）：

| 测试文件 | 用例数 | 对应上面第几组 |
| --- | --- | --- |
| `phase1/tests/test_smoke.py` | 1 | happy path（第 1 组） |
| `phase1/tests/test_db_path.py` | 6 | 第 8 组（路径解析）+ `get_db()` 建父目录 + 导出脚本共用同一路径 |
| `phase1/tests/test_claim_and_role.py` | 9 | 第 3 组（领取竞态）+ 第 4 组（角色 UPSERT）+ implementer 身份固化 |
| `phase1/tests/test_state_machine.py` | 21 | 第 1 组（状态机）+ 第 2 组（异常态可达）+ `evidence_path` |
| `phase1/tests/test_selection.py` | 21 | 第 6 组（依赖门禁）+ 契约字段与优先级排序 |
| `phase1/tests/test_failure_and_evidence.py` | 14 | 第 2 组（异常态写入）+ 失败处理与证据 |
| `phase1/tests/test_independence_guard.py` | 7 | 第 5 组（自验收守卫） |
| `phase1/tests/test_schema_migration.py` | 7 | 第 7 组（迁移兼容） |

测试依赖装在 `phase1/requirements-dev.txt`（`pytest>=8`），本机跑法：

```
.venv/Scripts/python.exe -m pytest phase1/tests -q
```

`phase1/tests/conftest.py` 把 pytest 的 `basetemp` 重定向到系统 TEMP 下的私有目录（`%TEMP%/ccs-pytest-basetemp`），绕开本机被其它账号残留污染的 `pytest-of-<user>` 目录；所有测试库都落在 pytest 自己的 `tmp_path` 里，不碰真实 `/tmp`、`C:/tmp` 或用户目录。

CI（protocol-ci.yml）同步改造：移除 `cli.py init || true` 的吞错与只 echo 的步骤，改为 `python -m pytest phase1/tests -q`，测试失败必须让工作流变红。**已实现（T-03）**：该工作流现在的 `permissions` 是 `contents: read`，安装 `phase1/requirements-dev.txt` 后真实执行 pytest，全文没有 `|| true`。

## 11. P1 验收标准——已实现（2026-11-17 验收）

- 上述 8 组测试全绿，CI 真实反映结果（无 `|| true`）。✅ 实测 86 passed，CI 无 `|| true`。
- v0 库自动迁移到 v1 且数据无损；旧脚本与适配器不改逻辑可运行。✅ `test_schema_migration.py` 覆盖迁移、幂等与旧数据不丢；既有函数签名未改。
- 老引擎的优先级、依赖、重试上限、blocked/needs_human、实现者身份与自验收禁止，在 phase1 都有对等且可测试的承载。✅ 分别由 `select_next_task` / `record_failure` / `block_task` / `request_human` / `atomic_claim(executor_id=)` / 自验收守卫承载。
- 所有新增状态变更均经 transition 并落 audit_log；无任何绕过协议层的直写 UPDATE（worktree 现有直写在 P1 顺带改为走审计，可作为本批可选清理项）。⚠️ 前半句成立；**worktree 的直写 UPDATE 仍未改** —— `create_worktree` / `remove_worktree` 依旧直接写 `tasks.worktree_path` 且不落审计，属本批未做的可选项。

## 12. P1 明确不做

- 不实现 codex/claude 真实调用、日志落盘、配额钩子（P2）。
- 不做 orchestrator 循环与 launcher（P3）。
- 不做 queue.json 数据迁移与任何割接（P4）。
- 不引入认证/复杂 RBAC；自验收仅字符串级守卫并如实标注局限。
- 不 push、不修改期货分析仓库与其任务计划。

以上边界在 T-01 ~ T-09 中均未被触碰：P1 没有引入真实 CLI 调用、编排循环或割接，也没有引入认证 / 复杂 RBAC。
