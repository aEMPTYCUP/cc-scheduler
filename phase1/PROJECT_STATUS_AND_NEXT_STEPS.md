# Multi-Agent Development Factory — 项目全貌与推进线路

这份文件给 Claude Code 和 Codex 实现用。目标是说明项目现在走到哪、接下来做什么、哪些事不能动。先把能跑的最小增量做出来，再优化。

## 1. 项目目标（一句话 + 核心原则）

**一句话：** 不是再造一套 AI 编程工具，而是把已经在用的 Claude Code、Codex、Cursor、豆包统一调度起来，用同一套任务池、同一套岗位规范和同一套验收标准干活。

核心原则：

- **统一调度现有 AI，不重新造工具。** 协议层只负责派活、约束、回收结果，执行仍落在现有 CLI 或人工复制粘贴上。
- **岗位与执行器解耦。** 岗位是 A0 / A3 / A7 / A8 这类角色规范；执行器是豆包、Codex、Claude、Cursor。同一个岗位可以换执行器，换执行器不能改写岗位职责。
- **开发与验收分离。** 实现者不能自己验收自己的活。A7 是独立质量门，通过之后才交给 A8 整合。
- **薄协议层掌握调度权和标准。** 状态变更、任务包、ROLE_SPEC、审计日志都走协议层。业务代码和执行器都不要绕开它自己改状态。

## 2. 当前已完成（Phase 1 + 1.5 + 最小 UI）

仓库在 `phase1/`。默认数据库是 `/tmp/phase1_protocol.db`。下面这些已经落地，并且多数已经跑通过，实现时请先复用，不要推倒重来。

### 协议层

- `phase1/protocol.py`：任务创建、READY、原子领取、状态流转、任务包生成、审计写入、worktree 绑定。
- `phase1/schema.sql`：`role_specs`、`tasks`、`audit_log`、`claims`。
- 状态机：`BACKLOG → READY → CLAIMED → IN_PROGRESS → SUBMITTED → REVIEW → (ACCEPTED | REWORK) → INTEGRATION → (REVIEW | DONE)`。`REWORK` 可以回到 `CLAIMED` 或 `IN_PROGRESS`。
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
- 已在 `workflow_dispatch` 且 `simulate=true` 下跑通，产物为 `protocol-audit`。
- 这只证明协议模拟链路能在 CI 里走完，不代表真实 CLI 已经在 CI 里跑通。

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

文件主要在 `phase1/ui/` 和 `phase1/scripts/export_for_ui.py`、`phase1/scripts/ui_server.py`。

已经有雏形、只需打磨补齐的：

- **历史任务切换**：页面上已有“进行中 / 全部与历史”。请检查空状态、已完成任务的展示是否清楚，不要另做一套切换。
- **一键复制提示词到剪贴板**：弹窗里已有复制按钮。请保证在常见浏览器里稳定可用，失败时能手动选中文本，并让用户看得出“已复制”。

本轮要新做的：

- **任务创建表单。** 在本地看板服务里增加最小创建能力：填写任务编号、标题、说明、验收标准、岗位（A0/A3/A7/A8），提交后走 `protocol.create_task`，必要时再 `ready_task`。创建成功后刷新看板。不要在纯 `file://` 打开时假装能写库；没有本地服务时，明确提示去跑 `ui_server.py` 或 CLI。
- **状态机可视化时间线。** 参考期货分析仓库里 `DevelopmentTasks.vue` 的六步展示（立项、任务派发、开发、交付检查、独立验收、提交完成）：左侧或详情里用一条时间线，标出已完成 / 当前 / 未开始，每一步显示执行者和时间。请把协议状态映射到清晰的步骤，例如：立项（BACKLOG/READY）、领取（CLAIMED）、开发（IN_PROGRESS）、提交（SUBMITTED）、独立验收（REVIEW/ACCEPTED/REWORK）、整合完成（INTEGRATION/DONE）。不要照搬期货分析那套读 JSON 文件的后端，数据仍来自本仓库的 `tasks` 和 `audit_log`。

验收一看就懂：打开看板，能看到任务现在走到哪一步、提示词能复制、新任务能从页面建出来。

### 线路 B：真实执行器接入（Phase 2 骨架）

文件主要在 `phase1/scripts/claude_adapter.py`、`phase1/scripts/cursor_adapter.py`。如需 Codex，可按同样风格新增适配器，不要另起一套状态机。

要做的事：

- **把真实 CLI 调用做成默认可运行路径。** `--simulate-success` 可以保留给 CI 和本地空跑，但不能再当唯一能走通的路径。真实调用要有超时（现有默认 600 秒可沿用或做成参数）、把 stdout/stderr/返回码落盘到明确目录（例如 `phase1/task-packages/<task_id>/logs/` 或 `phase1/logs/`），方便排查。
- **支持第三方 endpoint。** Claude 已在看 `ANTHROPIC_BASE_URL` / `CLAUDE_BASE_URL`。请把同样思路补到需要走 OpenAI 兼容接口的执行器上，使用 `OPENAI_BASE_URL`（以及对应 API Key 环境变量）。文档里写清要配哪些环境变量，不要把密钥写进仓库。
- **失败重试与人工接管。** CLI 超时或非 0 退出时，不要假装成功、不要贸然进 `REVIEW`。允许有限次重试；仍失败则保持或回写一个可识别的失败/待接管信息（必须通过协议层和审计，不要偷偷改字段）。人工接管后，可以回到看板上的复制提示词路径，由人继续做完再 `mark_submitted`。

豆包这条线本轮继续保持人工复制粘贴，不要在没有官方稳定 CLI 的情况下硬接。

本轮至少要有一条真实 CLI 路径：在配置好 Secrets / 环境变量后，能对一个试点任务真正调起 Claude 或 Cursor（或 Codex），并看到日志和状态回写结果。

### 线路 C：协议层加固

文件主要在 `phase1/protocol.py`、`phase1/scripts/worktree.py`、必要时小幅扩展 `schema.sql`。改协议层时必须保持现有函数签名和行为兼容，`cli.py`、适配器、看板脚本都依赖它们。

要做的事：

- **Worktree 与任务绑定自动化。** 领取任务时，按约定自动创建隔离 worktree，并把路径写入 `tasks.worktree_path`。任务 `DONE`（或确认废弃）后按规则清理。失败要写进审计，不要留下找不到的脏目录却不记录。
- **审计证据文件落盘。** `audit_log.evidence` 现在可以写文本。请再落到文件，例如 CLI 日志路径、提交 SHA、任务包哈希，让 A7 打开就能核对。证据路径写进审计记录，文件本身不要进无关的 git 提交，除非文档明确要求。
- **简单的权限检查。** 最低要求：实现者不能验收自己的工作；以 A7 身份做 `ACCEPTED` / `REWORK` 时，不能和该任务的实现者是同一个 actor。这一条先做成明确、可测试的规则，不要上复杂的权限系统。

## 5. 给实现者的注意事项

- **不要破坏现有协议层接口。** 优先扩展，不要改掉 `create_task`、`ready_task`、`atomic_claim`、`transition`、`get_task`、`list_tasks`、`get_audit`、`create_worktree` 这些已经在用的函数含义。如果必须改接口，先同步改调用方，并在本文件或 README 里写明。
- **豆包暂时保持人工复制粘贴。** 看板上生成提示词、人去执行、再标记已提交，这条路径要一直能用。
- **所有状态变更必须走 `protocol.transition` 并写入 `audit_log`。** 禁止脚本或页面对 SQLite 直接 `UPDATE tasks SET status=...`。看板的“标记为已提交”必须继续走 `mark_submitted.py` / 现有协议调用。
- **先做可运行的最小增量，再优化。** 每条线路先交出能演示的一小步，再加 polish。不要等三条线都“设计完”再合并。
- **密钥只走环境变量或 CI Secrets。** 不要写进源码、提交记录或演示 JSON。
- **文档和代码一起改。** 行为变了，就更新本文件和 `phase1/README.md`。

## 6. 验收标准（本轮）

这轮做完，至少要同时满足下面三条：

1. **UI 能完整展示任务 + 生成提示词 + 人工标记状态。** 任务列表、当前状态、最近审计、执行器选择、提示词复制都可用；新建任务如果做了，必须真实写入任务池并能在看板上看到。不要只改演示 JSON 却没走协议层。
2. **至少一条真实 CLI 调用路径，可在配置 Secrets 后跑通。** 不是只开 `--simulate-success`。要有超时、日志落盘，成功后按协议回写状态；失败不能冒充成功。
3. **文档与代码同步更新。** `phase1/README.md` 和本文件要反映真实用法：怎么开看板、怎么导出、怎么标记提交、怎么配置 endpoint、怎么跑适配器。

A7 仍然是质量门。实现者提交后，由 A7 按上面三条验收，通过才能进整合。
