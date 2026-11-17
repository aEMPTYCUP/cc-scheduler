# cc-scheduler 建设任务队列（phase1 取代 .claude 老引擎）

> 本文件是 cc-scheduler 项目的**独立任务队列**，与期货分析的 `tasks/queue.json` 物理隔离，
> 不被任何 15 分钟定时器驱动，由当前会话手动驱动。P1 建成后整体导入 phase1 协议层完成自举，本文件归档。
> 设计依据：`phase1/PHASE_2_REPLACEMENT.md`、`phase1/P1_SCHEMA_DESIGN.md`。

## 角色与铁律

- **CC（本会话）= A0 拆分 + A7 验收**：写契约、派单、独立审计、判 PASS/FAIL，不写业务代码。
- **Codex = 唯一实现者（A3）**：只在 allowed_paths 内改，不自我验收。
- 不信任 Codex 的完成摘要；验收只看 pytest 实际输出 + git diff + 独立 review。
- 状态：⬜ pending / 🔵 running / 🟡 awaiting_review / ✅ passed / 🔴 failed / 🙋 needs_human / ⛔ blocked。
- 返工：failed → 聚焦返工 prompt，`redo_count++`；同一任务 3 次不过 → ⛔ blocked 升级人工。
- 只在分支 `feature/phase1-p0-p1` 上工作；PASS 才 commit，**不 push**（push 需人工确认）。
- 派单前查 MiniMax 配额，fail-closed（异常/冷却即停，不硬调）。

## 实测环境备注（2026-11-17）

- 本机 `%TEMP%\pytest-of-32831\pytest-current` 因 codex 沙箱账号（unknown）残留出现 Permission denied，导致**默认 basetemp 的 pytest 在会话清理阶段退出码 1，但测试断言本身全过**。T-01 验收用 `--basetemp=$TEMP/ccs-pytest-bt` 取得权威 6 passed。**T-03 必须落地项目级 pytest 配置（addopts 指向仓库内被 gitignore 的临时目录），让后续任务不再依赖被污染的用户 TEMP。**
- codex 0.147 派单固定写法：`codex exec -C <repo> --approve-for-me --ephemeral --color never "<prompt>" </dev/null`。两条铁律：`--sandbox workspace-write` 与 `--approve-for-me` 互斥（后者自带 workspace-write）；后台运行必须 `</dev/null`，否则 codex 阻塞在 `Reading additional input from stdin...` 永不返回。
- 验收只提交明确文件，禁止 `git add -A`；`__pycache__/`、`.pytest_cache/` 由 T-02 进 .gitignore。
- **2026-11-17 T-05 内存回收事件（已恢复）**：后台 codex 曾因系统内存不足被 kill，但实现已完整落盘（`protocol.py` 状态机扩展 + `test_state_machine.py`）；恢复后**未重调 LLM**，仅本地独立验收（全量 35 passed、审 diff 确认严格只加边、非法转换回滚不写审计）即通过并提交。结论：被内存回收 ≠ 任务失败，优先验收已落盘成果，避免重复耗 token；**重派 codex 前先确认空闲内存充足**。
- 后台任务内存回收开关：若要避免后台 shell 因内存压力被自动 kill，需在**启动 Claude Code 时**设置环境变量 `CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP=1`（在 shell 里临时 export 无效）。
- **2026-11-17 T-06 实测修正（T-10 文档同步时处理）**：① register_role 的 INSERT OR REPLACE 在当前 NO ACTION 外键下**并不会立即报错**（语句末检查时新行已存在），真实危害是 delete-reinsert 使 rowid 漂移、以及未来若加 ON DELETE CASCADE 会静默删除引用任务——P1_SCHEMA_DESIGN.md 9.2 的"可能抛约束失败"措辞需改为"原地更新，避免 rowid 漂移与未来 CASCADE 风险"。② 独立 15 轮×8 线程（120 次领取）压测：败者全部走 SELECT 守卫抛 ValueError，`RuntimeError("Claim race lost")` 分支实际不可达但保留作防御；无 database-is-locked、无 flaky。
- **2026-11-17 T-07 scope 矛盾修正（非 codex 缺陷，redo=0）**：守卫按 §6.2 用 `claimed_by` 回退拦截单人自验收是正确的（独立验证拒绝审计可在抛 PermissionError 后存活），但旧 test_smoke/test_state_machine 用单 actor 跑全程并在 REVIEW 自验收，与新规则冲突导致 3 个测试失败。教训：**新增强制规则时，allowed_paths 必须同时包含需要随之更新的既有测试**，不能既要求"拦截 claimed_by 自验收"又要求"旧测试一字不改且全绿"。处理：放宽 scope 让 codex 把这 3 处改为独立验收人，protocol.py 守卫禁止再动。
- **2026-11-17 T-08 第二次内存回收（与 T-05 不同）**：本次 codex 仅完成基线确认（51 passed）就被 kill，**未写任何代码**（protocol.py 未改、test_selection.py 未生成），无半成品可本地验收，必须在内存缓解后**全新重派**。事件时空闲内存仅 19.3%（3.1GB）。结论：后台 codex 在空闲内存 <~25% 时被回收概率高；重派前应先把空闲内存释放到明显更高水平（建议 ≥5GB），或在启动 Claude Code 时设 `CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP=1` 关闭回收（需重启会话，且有系统内存耗尽风险）。

## 队列总表

| ID | 状态 | 优先级 | 依赖 | redo | 标题 |
| --- | --- | --- | --- | --- | --- |
| T-01 | ✅ passed | high | — | 0 | DB 路径单一来源 + PHASE1_DB env |
| T-02 | ✅ passed | medium | T-01 | 0 | .gitignore 补全与生成物退跟踪 |
| T-03 | ✅ passed | high | T-01 | 0 | pytest 骨架 + CI 改真跑测试 |
| T-04 | ✅ passed | high | T-03 | 0 | schema v1 + user_version 幂等迁移 |
| T-05 | ✅ passed | high | T-04 | 0 | 状态机三异常态 + evidence_path |
| T-06 | ✅ passed | high | T-04 | 0 | 修 rowcount/UPSERT 两 bug + implementer 身份 |
| T-07 | ✅ passed | high | T-06 | 0 | 自验收机械守卫 |
| T-08 | ✅ passed | high | T-04 | 0 | 契约字段 + 依赖表 + select_next_task |
| T-09 | ⬜ | high | T-05,T-06 | 0 | 失败处理 + task_evidence |
| T-10 | ⬜ | medium | 全部 | 0 | 全量回归 + 文档同步 |

---

## T-01 — DB 路径单一来源 + PHASE1_DB 环境变量

- 状态：⬜ pending ｜ 优先级：high ｜ 依赖：无
- **allowed_paths**：`phase1/protocol.py`、`phase1/scripts/export_for_ui.py`、`phase1/tests/test_db_path.py`（新建）
- **背景**：`protocol.py:13` 与 `export_for_ui.py:24` 各自硬编码 `Path("/tmp/phase1_protocol.db")`，本机实测解析为 `C:\tmp\...`，干净 Windows 无此目录会崩，且两处重复。
- **behavioral_check**：`cd phase1 && python -m pytest tests/test_db_path.py -q`
- **pass_condition**：
  1. protocol.py 提供 `_resolve_db_path()` 与模块级 `DB_PATH`；规则：设了 `PHASE1_DB` 环境变量则用之；否则若 legacy `/tmp/phase1_protocol.db` 已存在则回退它（不丢旧数据）；否则用 `%TEMP%/phase1/phase1_protocol.db`（平台临时目录，自动建父目录）。
  2. export_for_ui.py 删除自身硬编码，改为 `from protocol import DB_PATH as DEFAULT_DB`（其 import 路径已由 ui_server 注入 phase1 目录；需保证脚本独立运行时 sys.path 也可用）。
  3. 新增测试用 monkeypatch 环境变量与 tmp_path 覆盖三场景；不允许任何测试写真实 `/tmp` 或 `C:\tmp`。
  4. 既有函数签名与默认行为（无 env 且无 legacy 时之外的兼容）不变。

## T-02 — .gitignore 补全与生成物退跟踪

- 状态：⬜ pending ｜ 优先级：medium ｜ 依赖：T-01
- **allowed_paths**：`.gitignore`（根目录）、仅用 `git rm --cached` 处理 `phase1/ui/data.json`、`phase1/ui/data.js`
- **behavioral_check**：`git check-ignore phase1/ui/data.json phase1/ui/data.js phase1/task-packages/x phase1/logs/x.log`（每条都应命中）
- **pass_condition**：
  1. 忽略 `phase1/task-packages/`、`phase1/logs/`、`phase1/ui/data.json`、`phase1/ui/data.js`、`*.db`、`__pycache__/`、`.pytest_cache/`。
  2. `phase1/ui/demo_fallback.json` 必须继续被跟踪（演示基线，非生成物）。
  3. 已跟踪的 data.json/data.js 用 `git rm --cached` 退跟踪但保留本地文件；工作区文件不删除。

## T-03 — pytest 骨架 + CI 改真跑测试

- 状态：⬜ pending ｜ 优先级：high ｜ 依赖：T-01
- **allowed_paths**：`phase1/tests/`（新建 conftest.py、test_smoke.py）、`.github/workflows/protocol-ci.yml`、`phase1/requirements-dev.txt`（新建，可选）
- **behavioral_check**：`cd phase1 && python -m pytest -q` 本地绿；人为临时断言失败时退出码非 0
- **pass_condition**：
  1. conftest 提供 fixture，monkeypatch `protocol.DB_PATH` 到 tmp_path 下临时库，每个测试隔离、零 `/tmp` 依赖。
  2. 一条冒烟测试覆盖 init_db→create_task→ready→claim→走到 DONE 的 happy path。
  3. CI 删除 `cli.py init || true` 的吞错与只 echo 的假步骤，改为真实 `python -m pytest phase1/tests -q`，测试失败工作流必须变红。

## T-04 — schema v1 + user_version 幂等迁移

- 状态：⬜ pending ｜ 优先级：high ｜ 依赖：T-03
- **allowed_paths**：`phase1/schema.sql`、`phase1/protocol.py`、`phase1/tests/test_schema_migration.py`
- **behavioral_check**：`python -m pytest tests/test_schema_migration.py -q`
- **pass_condition**：按 P1_SCHEMA_DESIGN 第 3、4 节：executors/tasks 新列/task_dependencies/task_evidence/audit.evidence_path/索引；`ensure_schema()` 用 PRAGMA user_version；v0→v1 用 ADD COLUMN（迁移库无 CHECK，应用层校验）+ IF NOT EXISTS 建表 + executors 种子（INSERT OR IGNORE）；重复执行幂等；旧数据行数与审计不变。

## T-05 — 状态机三异常态 + evidence_path

- 状态：⬜ pending ｜ 优先级：high ｜ 依赖：T-04
- **allowed_paths**：`phase1/protocol.py`、`phase1/tests/test_state_machine.py`
- **behavioral_check**：`python -m pytest tests/test_state_machine.py -q`
- **pass_condition**：按设计第 5 节，只加边不删旧边；新增 FAILED/NEEDS_HUMAN/BLOCKED 合法转换；transition 增加 `evidence_path=None` 透传写入 audit_log；旧 10 态全部既有合法转换测试仍绿。

## T-06 — 修 rowcount/UPSERT 两 bug + implementer 身份

- 状态：⬜ pending ｜ 优先级：high ｜ 依赖：T-04
- **allowed_paths**：`phase1/protocol.py`、`phase1/tests/test_claim_and_role.py`
- **behavioral_check**：`python -m pytest tests/test_claim_and_role.py -q`
- **pass_condition**：
  1. atomic_claim 用 cursor.rowcount 判定竞态（替换 conn.total_changes），并发两连接仅一个成功。
  2. register_role 改 `INSERT ... ON CONFLICT DO UPDATE`，已有任务引用该 role 时重复注册不抛 FK 错且哈希更新。
  3. atomic_claim 新增可选 `executor_id=None`，提供时校验 executors.active 并固化到 tasks.implementer_executor_id；不传则维持旧行为。

## T-07 — 自验收机械守卫

- 状态：⬜ pending ｜ 优先级：high ｜ 依赖：T-06
- **allowed_paths**：`phase1/protocol.py`、`phase1/tests/test_independence_guard.py`
- **behavioral_check**：`python -m pytest tests/test_independence_guard.py -q`
- **pass_condition**：REVIEW 态做 ACCEPTED/REWORK 时，reviewer（actor_executor_id 或 actor）== implementer_executor_id/claimed_by 则抛 PermissionError、状态不变、写拒绝审计；不同身份可正常验收。文档注明"独立新会话"无法机械强制的局限。

## T-08 — 契约字段 + 依赖表 + select_next_task

- 状态：⬜ pending ｜ 优先级：high ｜ 依赖：T-04
- **allowed_paths**：`phase1/protocol.py`、`phase1/schema.sql`、`phase1/tests/test_selection.py`
- **behavioral_check**：`python -m pytest tests/test_selection.py -q`
- **pass_condition**：create_task 新增带默认值的 kwargs（priority/dependencies/allowed_paths/behavioral_check/pass_condition/max_retries）；写 task_dependencies；select_next_task 只返回依赖全 DONE 的 READY 任务，按 priority(high>medium>low)→created_at 排序且不改状态；旧调用签名兼容。

## T-09 — 失败处理 + task_evidence

- 状态：⬜ pending ｜ 优先级：high ｜ 依赖：T-05,T-06
- **allowed_paths**：`phase1/protocol.py`、`phase1/schema.sql`、`phase1/tests/test_failure_and_evidence.py`
- **behavioral_check**：`python -m pytest tests/test_failure_and_evidence.py -q`
- **pass_condition**：record_failure 使 retry_count++、写 last_error 与审计，未超限落 FAILED、达 max_retries 落 BLOCKED；block_task/request_human/release_claim（写 claims.released_at）；add_evidence 写 task_evidence 并可同步 audit.evidence_path。

## T-10 — 全量回归 + 文档同步

- 状态：⬜ pending ｜ 优先级：medium ｜ 依赖：以上全部
- **allowed_paths**：`phase1/tests/`、`phase1/README.md`、`phase1/PROJECT_STATUS_AND_NEXT_STEPS.md`
- **behavioral_check**：`cd phase1 && python -m pytest -q` 全绿
- **pass_condition**：P1 设计第 10 节 8 组测试齐且全绿；cli/adapters/ui_server/assisted_pilot/mark_submitted 不改逻辑可运行（兼容性回归）；README 与状态文档反映真实用法与已知局限。
