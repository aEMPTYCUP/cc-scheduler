# Multi-Agent Development Factory — 项目全貌与推进线路

这份文件给 Claude Code 和 Codex 实现用。目标是说明项目现在走到哪、接下来做什么、哪些事不能动。先把能跑的最小增量做出来，再优化。

## 0. P1 完成状态（2026-11-17）

P0/P1 的九个编码任务 T-01 ~ T-09 已全部完成并通过独立验收。协议层从“能跑通的最小链路”补平为“带迁移、带异常态、带守卫、带证据”的正式协议层。本节是**当前状态的权威描述**；下面第 2 ~ 6 节是历史记录，保留不删，与本节冲突的过时表述已就地更正。

### 0.1 测试现状（实测，非估计）

```
.venv/Scripts/python.exe -m pytest phase1/tests -q
86 passed in 9.17s
```

8 个测试文件、**86 条用例全绿，退出码 0**（上面的输出是实跑结果，耗时随机器波动）：

| 文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `phase1/tests/test_smoke.py` | 1 | init → create → ready → claim → DONE 的完整链路 |
| `phase1/tests/test_db_path.py` | 6 | `PHASE1_DB` / legacy / 临时目录三场景 + `get_db()` 建父目录 |
| `phase1/tests/test_claim_and_role.py` | 9 | 领取竞态、角色 UPSERT、implementer 身份固化 |
| `phase1/tests/test_state_machine.py` | 21 | 14 条新增合法边、3 条非法边、旧 10 态回归、`evidence_path` |
| `phase1/tests/test_selection.py` | 21 | 契约字段、优先级排序、依赖门禁、`select_next_task` 只读 |
| `phase1/tests/test_failure_and_evidence.py` | 14 | `record_failure` / `block_task` / `request_human` / `release_claim` / `add_evidence` |
| `phase1/tests/test_independence_guard.py` | 7 | 自验收守卫拒绝 / 放行 / 局限声明 |
| `phase1/tests/test_schema_migration.py` | 7 | fresh install、v0→v1 迁移、幂等、旧数据不丢 |

CI（`.github/workflows/protocol-ci.yml`，工作流名 “Multi-Agent Protocol CI”）已改为真实跑 pytest：安装 `phase1/requirements-dev.txt`（`pytest>=8`），执行 `python -m pytest phase1/tests -q`，**没有 `|| true` 吞错**，测试失败工作流必红；`permissions` 收敛为 `contents: read`。

### 0.2 T-01 ~ T-09 已交付能力

| 任务 | 交付内容 | 落点 |
| --- | --- | --- |
| T-01 | 数据库路径单一来源：`_resolve_db_path()` 优先级为 `PHASE1_DB` 环境变量 > 已存在的 legacy `/tmp/phase1_protocol.db` > 系统临时目录 `%TEMP%/phase1/phase1_protocol.db`；`get_db()` 自动建父目录 | `phase1/protocol.py` |
| T-02 | `.gitignore` 补全：`phase1/task-packages/`、`phase1/logs/`、`phase1/ui/data.json`、`phase1/ui/data.js`、`*.db`、`__pycache__/`、`.pytest_cache/` 退跟踪；`phase1/ui/demo_fallback.json` 保持被跟踪 | 根目录 `.gitignore` |
| T-03 | pytest 骨架 + CI 改真跑测试：依赖落在 `phase1/requirements-dev.txt`；`conftest.py` 把 `basetemp` 重定向到系统 TEMP 下的私有目录，绕开本机被污染的 `pytest-of-<user>` | `phase1/tests/`、`.github/workflows/protocol-ci.yml` |
| T-04 | schema v1 + `PRAGMA user_version` 幂等迁移：新表 `executors` / `task_dependencies` / `task_evidence`；`tasks` 新增 `priority` / `dependencies_json` / `executor_id` / `implementer_executor_id` / `retry_count` / `max_retries` / `allowed_paths_json` / `behavioral_check` / `pass_condition` / `last_error`；`audit_log` 新增 `evidence_path`；`claims.released_at` 启用 | `phase1/schema.sql`、`phase1/protocol.py` |
| T-05 | 状态机三异常态 `FAILED` / `NEEDS_HUMAN` / `BLOCKED` 及其合法边（只加边、不删旧边）；`transition` 支持 `evidence_path` | `phase1/protocol.py` |
| T-06 | 修两个既有 bug：`atomic_claim` 竞态判定改用 `cursor.rowcount`（不再用连接累计 `total_changes`）；`register_role` 改 `INSERT ... ON CONFLICT DO UPDATE` 原地更新。另加 `atomic_claim(executor_id=...)` 固化实现者身份 | `phase1/protocol.py` |
| T-07 | 自验收机械守卫：`transition` 支持 `actor_executor_id`；REVIEW 态做 `ACCEPTED` / `REWORK` 时 reviewer == implementer 抛 `PermissionError`，拒绝记录以自环审计先 commit 再抛错 | `phase1/protocol.py` |
| T-08 | 结构化验收契约 + 依赖表 + 选单：`create_task` 新增带默认值的 kwargs；依赖在创建时必须已存在；写 `task_dependencies` 与去重排序的 `dependencies_json`；`select_next_task()` 只读、依赖全 DONE 才合格、按 priority → created_at → task_id 排序 | `phase1/protocol.py`、`phase1/schema.sql` |
| T-09 | 失败处理与证据：`record_failure`（`retry_count`+1、写 `last_error`，未超限落 `FAILED`、达 `max_retries` 落 `BLOCKED`，默认 3 次）、`block_task`、`request_human`、`release_claim`（只打 `claims.released_at`，不改任务状态）、`add_evidence`（kind 白名单 + 可选同步 `audit.evidence_path`） | `phase1/protocol.py` |

### 0.3 三条线路的现状

- **线路 C（协议层加固）：已基本完成。** T-04 ~ T-09 覆盖了它要求的 schema 迁移、审计证据字段、异常态与最小权限检查。**唯一没做**的是「领取任务时自动创建隔离 worktree 并在 DONE 后清理」——`protocol.create_worktree` / `remove_worktree` 仍是手动调用。
- **线路 A（看板 UI）：仍未做。** 看板还是 Phase 1 的静态导出 + 本地服务：没有任务创建表单，没有状态机可视化时间线。按 `PHASE_2_REPLACEMENT.md` 第 7 节，线路 A 不在接班关键路径上，降级为并行的可视层；P4 割接后它会成为观察内核的主要窗口。
- **线路 B（真实执行器接入）：仍未做。** `phase1/scripts/claude_adapter.py`、`phase1/scripts/cursor_adapter.py` 仍是骨架，真实 CLI 调用、超时、日志落盘、重试与人工接管都没落地。按 `PHASE_2_REPLACEMENT.md` 的路线图，这一块归到 **P2（真实执行器 + 验收门禁）**，其后是 **P3（编排循环 orchestrator driver）** 与 **P4（影子运行 → 割接 → 退役）**；P2 的首要适配器按该文件要求由 claude/cursor 调整为 **codex**。

### 0.4 已知局限（如实保留，不要在文档里夸大）

- 自验收守卫只能做**字符串级** `actor != implementer` 校验：A7 要求的“独立新会话、不 resume 实现者对话”**无法由状态机机械证明**，只能靠岗位纪律加审计声明。
- 迁移库（v0→v1）的新列**没有库级 CHECK**（SQLite `ALTER TABLE ADD COLUMN` 不支持附带 CHECK），合法性靠应用层校验；只有 fresh install 才带完整 CHECK。
- `release_claim` 只打 `claims.released_at`，不改任务状态——状态机没有 `CLAIMED → READY` 边。
- `create_task` 的依赖**不支持前向引用、不支持自依赖**：被依赖任务必须先存在。

下一阶段看 `PHASE_2_REPLACEMENT.md` 的 P2 / P3 / P4。

## 1. 项目目标（一句话 + 核心原则）

**一句话：** 不是再造一套 AI 编程工具，而是把已经在用的 Claude Code、Codex、Cursor、豆包统一调度起来，用同一套任务池、同一套岗位规范和同一套验收标准干活。

核心原则：

- **统一调度现有 AI，不重新造工具。** 协议层只负责派活、约束、回收结果，执行仍落在现有 CLI 或人工复制粘贴上。
- **岗位与执行器解耦。** 岗位是 A0 / A3 / A7 / A8 这类角色规范；执行器是豆包、Codex、Claude、Cursor。同一个岗位可以换执行器，换执行器不能改写岗位职责。
- **开发与验收分离。** 实现者不能自己验收自己的活。A7 是独立质量门，通过之后才交给 A8 整合。
- **薄协议层掌握调度权和标准。** 状态变更、任务包、ROLE_SPEC、审计日志都走协议层。业务代码和执行器都不要绕开它自己改状态。

## 2. 当前已完成（Phase 1 + 1.5 + 最小 UI）

仓库在 `phase1/`。数据库路径不再写死 `/tmp`：`protocol._resolve_db_path()` 的优先级是 `PHASE1_DB` 环境变量 > 已存在的 legacy `/tmp/phase1_protocol.db` > 系统临时目录 `%TEMP%/phase1/phase1_protocol.db`，`get_db()` 会自动建父目录。下面这些已经落地，并且多数已经跑通过，实现时请先复用，不要推倒重来。

### 协议层

- `phase1/protocol.py`：任务创建、READY、原子领取、状态流转、任务包生成、审计写入、worktree 绑定。
- `phase1/schema.sql`：`role_specs`、`tasks`、`audit_log`、`claims`；P1 新增 `executors`、`task_dependencies`、`task_evidence` 三张表，以及 `tasks` / `audit_log` 的新列（见第 0 节与 `P1_SCHEMA_DESIGN.md` 第 3、4 节）。
- 状态机：`BACKLOG → READY → CLAIMED → IN_PROGRESS → SUBMITTED → REVIEW → (ACCEPTED | REWORK) → INTEGRATION → (REVIEW | DONE)`。`REWORK` 可以回到 `CLAIMED` 或 `IN_PROGRESS`。P1 在原有 10 态上增量加入 `FAILED`、`NEEDS_HUMAN`、`BLOCKED` 三个异常态，旧边一条未删。
- 任务包写在 `phase1/task-packages/<task_id>/`，至少包含 `ROLE_SPEC.md` 和 `manifest.json`（含 `role_spec_sha256`）。
- 所有状态变化都写 `audit_log`。本地已做过端到端验证：任务可以从创建走到 `DONE`，审计链路完整。
- 命令入口：`phase1/cli.py`。

### 角色规范

已有四份 `AGENT.md`，是岗位的权威来源：

- `phase1/roles/A0-orchestrator/AGENT.md`
- `phase1/roles/A3-backend-engineer/AGENT.md`
- `phase1/roles/A7-code-reviewer/AGENT.md`
- `phase1/roles/A8-integration-release/AGENT.md`

### 辅助脚本

- `phase1/scripts/assisted_pilot.py`：Phase 1.5。系统生成任务包和可复制提示词，人在豆包或 Cursor 里干活，确认后自动回写到 `SUBMITTED`，再进 `REVIEW`。A7 仍是验收门。
- `phase1/scripts/worktree.py`：给任务创建 / 清理 / 列出 git worktree，路径会写回任务记录。目前是手动调用，还没有和领取、完成自动绑死。
- `phase1/scripts/claude_adapter.py`、`phase1/scripts/cursor_adapter.py`：已能拼提示词、调用 CLI、超时返回、并用 `--simulate-success` 做自动回写。Claude 适配器已识别 `ANTHROPIC_BASE_URL` / `CLAUDE_BASE_URL`。真实 CLI 路径还没有作为默认、稳定的交付来验收。
- `phase1/scripts/pilot.py`：早期试点脚本。

### CI

- `.github/workflows/protocol-ci.yml`，工作流名是 “Multi-Agent Protocol CI”。
- **P1 起改为真实跑测试**：安装 `phase1/requirements-dev.txt`（`pytest>=8`），执行 `python -m pytest phase1/tests -q`，没有 `|| true` 吞错，测试失败工作流必红；`permissions` 收敛为 `contents: read`。
- 仍然没有真实 CLI 调用进入 CI：这条链路只证明协议层测试在 CI 里过，不代表真实 CLI 已经在 CI 里跑通。

### 最小看板

相关文件：

- `phase1/ui/index.html`：静态看板。
- `phase1/scripts/export_for_ui.py`：从 SQLite 导出 `phase1/ui/data.json` 和 `data.js`。没有库时写入演示数据。导出内容已包含角色摘要、`prompt_base`、最近一条审计。
- `phase1/scripts/ui_server.py`：本地服务，默认 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)，用于“标记为已提交”。
- `phase1/scripts/mark_submitted.py`：把任务推到 `SUBMITTED`。若当前是 `CLAIMED`，会先到 `IN_PROGRESS`，再到 `SUBMITTED`，不会自动进入 `REVIEW`。

已经支持：

- 进行中 / 全部与历史 切换
- 执行器下拉：豆包、Codex、Claude、Cursor
- 生成可复制提示词（任务说明 + ROLE_SPEC 摘要 + SHA256），弹窗里可复制到剪贴板
- 展示当前状态和最近一条审计
- 在本地服务下把任务标记为已提交

直接打开 `phase1/ui/index.html` 就可以复制提示词。改状态需要先启动 `ui_server.py`，或手动跑 `mark_submitted.py`。

### 相关 commit 范围

实现时请以 `master` 最新提交为准。和本阶段协议层、辅助流程、看板相关的提交，大致从 `cc209e4`（补上 `protocol.py`）之后，到 `639311f`（看板执行器、提示词和标记提交）附近。再往前还有 schema、CLI、适配器、CI 和 Phase 1.5。不要回退这些已合并的提交。

## 3. 当前架构简图（文字）

当前主路径是这样的：

A0 调度立项  
→ 任务进入 SQLite 任务池（`BACKLOG` / `READY`）  
→ 人工或脚本领取，生成带 ROLE_SPEC 的任务包（`CLAIMED`）  
→ 执行器干活：豆包目前是看板上生成提示词、人工复制粘贴；Claude / Cursor / Codex 的 CLI 还在骨架阶段  
→ 状态回写（`IN_PROGRESS` → `SUBMITTED`，看板标记提交停在这一步；适配器成功时还会进 `REVIEW`）  
→ A7 独立验收（`ACCEPTED` 或 `REWORK`）  
→ A8 整合发布（`INTEGRATION` → `DONE`）

协议层是唯一允许改状态的地方。看板、适配器、CLI 都只能调用它，不能直接改数据库里的 `status`。

## 4. 下一步推进线路（按优先级，给 Claude/Codex 实现）

下面三条可以并行。先做各自的最小可运行增量，不要互相等一个“完整方案”。改之前先读现有文件，能复用的接口不要重新发明。

### 线路 A：完善 UI（最优先，用户可见）

> **现状（2026-11-17）：仍未做。** 看板还是 Phase 1 的静态导出 + 本地服务（`export_for_ui.py` / `ui_server.py` / `mark_submitted.py`）。下面列的「任务创建表单」与「状态机可视化时间线」都还没有实现。按 `PHASE_2_REPLACEMENT.md` 第 7 节，线路 A 不在接班关键路径上，降级为并行的可视层。

文件主要在 `phase1/ui/` 和 `phase1/scripts/export_for_ui.py`、`phase1/scripts/ui_server.py`。

已经有雏形、只需打磨补齐的：

- **历史任务切换**：页面上已有“进行中 / 全部与历史”。请检查空状态、已完成任务的展示是否清楚，不要另做一套切换。
- **一键复制提示词到剪贴板**：弹窗里已有复制按钮。请保证在常见浏览器里稳定可用，失败时能手动选中文本，并让用户看得出“已复制”。

本轮要新做的：

- **任务创建表单。** 在本地看板服务里增加最小创建能力：填写任务编号、标题、说明、验收标准、岗位（A0/A3/A7/A8），提交后走 `protocol.create_task`，必要时再 `ready_task`。创建成功后刷新看板。不要在纯 `file://` 打开时假装能写库；没有本地服务时，明确提示去跑 `ui_server.py` 或 CLI。
- **状态机可视化时间线。** 参考期货分析仓库里 `DevelopmentTasks.vue` 的六步展示（立项、任务派发、开发、交付检查、独立验收、提交完成）：左侧或详情里用一条时间线，标出已完成 / 当前 / 未开始，每一步显示执行者和时间。请把协议状态映射到清晰的步骤，例如：立项（BACKLOG/READY）、领取（CLAIMED）、开发（IN_PROGRESS）、提交（SUBMITTED）、独立验收（REVIEW/ACCEPTED/REWORK）、整合完成（INTEGRATION/DONE）。不要照搬期货分析那套读 JSON 文件的后端，数据仍来自本仓库的 `tasks` 和 `audit_log`。

验收一看就懂：打开看板，能看到任务现在走到哪一步、提示词能复制、新任务能从页面建出来。

### 线路 B：真实执行器接入（Phase 2 骨架）

> **现状（2026-11-17）：仍未做。** `claude_adapter.py` / `cursor_adapter.py` 仍是骨架，真实 CLI 调用、超时、日志落盘、重试与人工接管都没落地。按 `PHASE_2_REPLACEMENT.md` 的路线图，这部分归到 **P2（真实执行器 + 验收门禁）**，其后是 **P3（编排循环）** 与 **P4（影子运行 → 割接 → 退役）**；P2 的首要适配器按该文件要求由 claude/cursor 调整为 **codex**。

文件主要在 `phase1/scripts/claude_adapter.py`、`phase1/scripts/cursor_adapter.py`。如需 Codex，可按同样风格新增适配器，不要另起一套状态机。

要做的事：

- **把真实 CLI 调用做成默认可运行路径。** `--simulate-success` 可以保留给 CI 和本地空跑，但不能再当唯一能走通的路径。真实调用要有超时（现有默认 600 秒可沿用或做成参数）、把 stdout/stderr/返回码落盘到明确目录（例如 `phase1/task-packages/<task_id>/logs/` 或 `phase1/logs/`），方便排查。
- **支持第三方 endpoint。** Claude 已在看 `ANTHROPIC_BASE_URL` / `CLAUDE_BASE_URL`。请把同样思路补到需要走 OpenAI 兼容接口的执行器上，使用 `OPENAI_BASE_URL`（以及对应 API Key 环境变量）。文档里写清要配哪些环境变量，不要把密钥写进仓库。
- **失败重试与人工接管。** CLI 超时或非 0 退出时，不要假装成功、不要贸然进 `REVIEW`。允许有限次重试；仍失败则保持或回写一个可识别的失败/待接管信息（必须通过协议层和审计，不要偷偷改字段）。人工接管后，可以回到看板上的复制提示词路径，由人继续做完再 `mark_submitted`。

豆包这条线本轮继续保持人工复制粘贴，不要在没有官方稳定 CLI 的情况下硬接。

本轮至少要有一条真实 CLI 路径：在配置好 Secrets / 环境变量后，能对一个试点任务真正调起 Claude 或 Cursor（或 Codex），并看到日志和状态回写结果。

### 线路 C：协议层加固

> **现状（2026-11-17）：本线路已基本由 T-04 ~ T-09 落地**，交付内容见第 0 节。下面三条里：「简单的权限检查」已完成（T-07 自验收守卫）；「审计证据文件落盘」目前只完成协议层部分（`audit_log.evidence_path` + `task_evidence` + `add_evidence`），把真实 CLI 的 stdout/stderr 落盘到 `task-packages/<id>/logs/` 属于 P2；「Worktree 与任务绑定自动化」仍未做。

文件主要在 `phase1/protocol.py`、`phase1/scripts/worktree.py`、必要时小幅扩展 `schema.sql`。改协议层时必须保持现有函数签名和行为兼容，`cli.py`、适配器、看板脚本都依赖它们。

要做的事：

- **Worktree 与任务绑定自动化。**（⏳ 未做：`create_worktree` / `remove_worktree` 仍是手动调用）
- **审计证据文件落盘。**（✅ 协议层部分已完成：`audit_log.evidence_path` + `task_evidence`；真实 CLI 日志落盘属 P2）
- **简单的权限检查。**（✅ 已完成，见第 0 节 T-07；仅字符串级守卫）

## 5. 给实现者的注意事项

- **不要破坏现有协议层接口。** 优先扩展，不要改掉 `create_task`、`ready_task`、`atomic_claim`、`transition`、`get_task`、`list_tasks`、`get_audit`、`create_worktree` 这些已经在用的函数含义。如果必须改接口，先同步改调用方，并在本文件或 README 里写明。
- **豆包暂时保持人工复制粘贴。** 看板上生成提示词、人去执行、再标记已提交，这条路径要一直能用。
- **所有状态变更必须走 `protocol.transition` 并写入 `audit_log`。** 禁止脚本或页面对 SQLite 直接 `UPDATE tasks SET status=...`。看板的“标记为已提交”必须继续走 `mark_submitted.py` / 现有协议调用。
- **先做可运行的最小增量，再优化。** 每条线路先交出能演示的一小步，再加 polish。不要等三条线都“设计完”再合并。
- **密钥只走环境变量或 CI Secrets。** 不要写进源码、提交记录或演示 JSON。
- **文档和代码一起改。** 行为变了，就更新本文件和 `phase1/README.md`。

## 6. 验收标准（本轮）

> **现状（2026-11-17）：本轮三条验收标准中，第 3 条（文档与代码同步更新）已由 T-10 落实；第 1 条（看板创建表单与状态时间线）属于线路 A，第 2 条（真实 CLI 路径）属于线路 B，两条均未完成，排序见 `PHASE_2_REPLACEMENT.md` 的 P2 / P3 / P4。

这轮做完，至少要同时满足下面三条：

1. **UI 能完整展示任务 + 生成提示词 + 人工标记状态。** 任务列表、当前状态、最近审计、执行器选择、提示词复制都可用；新建任务如果做了，必须真实写入任务池并能在看板上看到。不要只改演示 JSON 却没走协议层。
2. **至少一条真实 CLI 调用路径，可在配置 Secrets 后跑通。** 不是只开 `--simulate-success`。要有超时、日志落盘，成功后按协议回写状态；失败不能冒充成功。
3. **文档与代码同步更新。** `phase1/README.md` 和本文件要反映真实用法：怎么开看板、怎么导出、怎么标记提交、怎么配置 endpoint、怎么跑适配器。

A7 仍然是质量门。实现者提交后，由 A7 按上面三条验收，通过才能进整合。
