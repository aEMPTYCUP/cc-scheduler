# Phase 2 — 用 phase1 协议层取代 .claude 老调度引擎（路线图）

> 状态：设计稿，供评审。本文件只描述方向与计划，不含已落地代码。
> 关联：P1 的具体表结构与函数改动见 [P1_SCHEMA_DESIGN.md](P1_SCHEMA_DESIGN.md)。
> 决策来源：确认 phase1 最终取代 `.claude/` 那套基于 `tasks/queue.json` 的调度引擎，成为**唯一调度内核**。

## 1. 背景：仓库里现存两套互不相通的调度系统

| 维度 | `phase1/`（新协议层，接班者） | `.claude/`（老引擎，在期货分析上实跑） |
| --- | --- | --- |
| 存储 | SQLite `phase1_protocol.db` | JSON `tasks/queue.json`（`.claude/tools/task_queue.py:34`） |
| 状态机 | BACKLOG…DONE 共 10 态，全是顺利路径 | pending/running/awaiting_review/passed/failed/needs_human/blocked 共 7 态，含 3 个异常态 |
| 任务 ID | 业务自定义（T-PILOT-001 等） | 自增 T-001（task_queue.py:59） |
| 选任务 | 手动 READY + atomic_claim，无优先级/依赖 | priority 排序 + 依赖门禁（task_queue.py:80-97） |
| 失败处理 | 无；适配器超时/非 0 仅 print，任务静默卡住 | redo_count，3 次返工→blocked（scheduler-prompt.md:79） |
| 独立验收 | A7 仅 AGENT.md 文字约束，transition 无强制 | acceptance-auditor 子代理 + agent-audits CLI，三层门禁 + UNCERTAIN |
| 验收契约 | `acceptance_criteria` 一段自由文本 | allowed_paths / behavioral_check / pass_condition 结构化 |
| 证据 | audit_log.evidence 一个文本列 | `.agent-audits/evidence/EV-*-diff.patch` 文件落盘 |
| 配额 | 无 | minimax_quota.py，fail-closed（异常即 should_pause） |
| 执行器 | claude/cursor 适配器骨架，simulate 才稳；无 codex | codex.exe `exec --non-interactive` 为真正主力 |
| 调度循环 | 无，全手动 | 任务计划每 15 分钟经 scheduler_launcher.py 唤醒一轮 |
| 看板 | 静态 HTML + 本地 submit 服务 | 无 |

结论：老引擎的**异常处理纪律**（重试上限、blocked、needs_human、独立验收、证据落盘、配额 fail-closed）是经过实战的资产；phase1 的资产是干净的状态机、任务包哈希绑定、审计表和看板。取代 = 把老引擎的纪律**搬进** phase1 的内核，而不是重写一遍。

## 2. 设计原则

1. **绞杀者模式（strangler fig），禁止大爆炸切换。** phase1 先在本仓库 dogfood 或测试项目上影子运行，证明能跑完整真实周期；在此之前不修改期货分析的 `.claude/`、任务计划或 `queue.json`。
2. **状态与审计归 phase1 独占。** 所有状态变更只走 `protocol.transition` 并写 audit_log；执行器、看板、适配脚本一律不得直接改库。
3. **执行器可插拔。** 岗位（A0/A3/A7/A8）与执行器（doubao/codex/claude/cursor/human）解耦；老引擎的 codex、配额、验收模块改造成 phase1 的适配器/门禁，接口化、可替换。
4. **迁移期人工路径始终可用。** 豆包复制粘贴 + `mark_submitted` 的 Phase 1.5 路径在整个取代过程中保持不破坏。
5. **向后兼容优先。** 不改变 `create_task / ready_task / atomic_claim / transition / get_task / list_tasks / get_audit / create_worktree` 的现有含义，只做增量（新参数带默认值、新状态只加边）。

## 3. 目标分层架构

```
┌─────────────────────────────────────────────────────┐
│ 看板层  ui/index.html + ui_server.py（只读视图 + 人工动作）│
├─────────────────────────────────────────────────────┤
│ 编排层  orchestrator driver（P3）                      │
│   配额闸门 → 选下一个任务 → 派发 → 回收 → 触发验收       │
│   确定性步骤用 Python；契约生成/拆解/UNCERTAIN 用 LLM     │
├──────────────┬──────────────┬───────────────────────┤
│ 执行器适配层  │  验收门禁层    │  配额/可用性层           │
│ codex/claude │ acceptance-  │ executor availability │
│ /cursor/人工  │  auditor 接入 │  hook（MiniMax 为其一） │
├──────────────┴──────────────┴───────────────────────┤
│ 协议内核（唯一可写状态）protocol.py + SQLite            │
│  状态机 / 审计 / 任务包哈希 / 契约 / 证据索引 / worktree  │
└─────────────────────────────────────────────────────┘
```

老引擎组件的归宿：

- `acceptance-auditor.md` + agent-audits CLI → 成为 phase1 的 REVIEW 门禁实现（初期**包装复用，不重写**）。
- `task_queue.py` 的 redo/blocked/优先级/依赖语义 → 落到 phase1 schema 与状态机（P1）。
- `minimax_quota.py` → 抽象为"执行器可用性钩子"，MiniMax 是第一个实现，保留 fail-closed。
- codex `exec --non-interactive` → 新增 `codex_adapter.py`，成为**首要真实适配器**。
- `scheduler-prompt.md` 六步循环 → P3 的 orchestrator driver，把"改 queue.json"全部换成调 protocol。
- `scheduler_launcher.py` 的绝对路径债（`期货分析` ROOT、codex.exe、npm 全局 CLI）→ 由执行器注册表/环境变量取代。

## 4. 状态机映射与缺口

老引擎 7 态映射到 phase1：

| 老引擎（queue.json） | phase1 对应 | 说明 |
| --- | --- | --- |
| pending | READY（依赖未满足时留在 BACKLOG） | 依赖门禁后才 READY |
| running | IN_PROGRESS | |
| awaiting_review | SUBMITTED / REVIEW | |
| passed | ACCEPTED → DONE | |
| failed | **缺，需新增 FAILED** | 单次尝试失败、可重试 |
| needs_human | **缺，需新增 NEEDS_HUMAN** | UNCERTAIN / 执行器不可用 / 配额冷却需人 |
| blocked | **缺，需新增 BLOCKED** | 重试耗尽或不可解决，需 A0/owner |

三个异常态是接班的硬缺口，P1 以**只加边、不改旧边**的方式引入，详见 P1 设计文档第 5 节。

## 5. 路线图

### P0 地基（无业务行为变更）
- DB 路径单一来源化：`PHASE1_DB` 环境变量 + 平台默认，兼容现有 `/tmp/phase1_protocol.db`；消除 protocol.py 与 export_for_ui.py 两处重复硬编码（实测在本机解析为 `C:\tmp\...`，干净 Windows 无此目录会崩）。
- 补 `.gitignore`：`task-packages/`、日志、生成物 `ui/data.json`、`ui/data.js`（保留 `demo_fallback.json`）。
- 建 pytest 骨架并接入 CI；去掉 protocol-ci.yml 里的 `|| true` 和只 echo 的假步骤，让 CI 真跑测试。
- 出口：CI 红/绿能真实反映协议层正确性。

### P1 协议补平（本路线图的脊柱，详见 P1_SCHEMA_DESIGN.md）
- schema 增量：priority、依赖、retry_count/max_retries、结构化契约字段、执行器注册表、证据表、`user_version` 迁移机制。
- 状态机增量：FAILED / NEEDS_HUMAN / BLOCKED 及合法边。
- 执行器身份 + 自验收机械守卫（A7 决策 actor ≠ 实现者）。
- 修两个既有 bug：`total_changes` 误用（protocol.py:108）、register_role 的 `INSERT OR REPLACE` 外键隐患。
- 出口：老引擎的队列/重试/阻塞/身份语义在 phase1 有对等承载，且旧接口与旧数据不破坏。

### P2 真实执行器 + 验收门禁
- 新增 `codex_adapter.py`（首要），claude/cursor 适配器补齐：超时、stdout/stderr/返回码落盘到 `task-packages/<id>/logs/`、有限重试、endpoint 环境变量（ANTHROPIC_* / OPENAI_*），密钥只走环境变量。
- 失败不冒充成功：重试耗尽走 P1 的 BLOCKED，全程写审计。
- acceptance-auditor 接入 REVIEW 转换；diff patch、commit SHA、日志路径写入 task_evidence。
- 配额钩子在派发前调用，冷却时任务不进入失败态（停在 READY/CLAIMED），只暂停派发。
- 出口：配置 Secrets 后，至少一条真实 CLI 路径端到端跑通，含一次真实 FAIL→返工→PASS。

### P3 编排循环（orchestrator driver）
- 确定性内核：查配额 → `select_next_task`（优先级+依赖）→ atomic_claim → 适配派发 → 回收 → 触发验收 → 提交或返工/阻塞。
- LLM 保留职责：验收契约生成（agent-audits plan）、backlog 拆解、UNCERTAIN 裁决。
- 驱动方式沿用 launcher 思路但路径全部配置化，日志落盘。
- 出口：一轮循环可无人值守完成"取任务→实现→独立验收→提交（不 push）"。

### P4 影子运行 → 割接 → 退役
1. 在 cc-scheduler 本仓库 dogfood 影子运行，覆盖：真实失败重试、blocked 升级、needs_human 接管、配额暂停、独立验收拦截自造缺陷（复用 pilot.py:40 的埋缺陷场景）。
2. 选一个非关键项目割接，对照老引擎并行观察至少一个完整工作周。
3. 写 `queue.json → SQLite` 一次性迁移脚本（按第 4 节映射，处理 T-001 与 phase1 ID 命名空间、在途任务）。
4. 最后才停用期货分析的 Task Scheduler 条目与 scheduler_launcher，归档 queue.json。
- 出口：老引擎停用后，phase1 独立承载全部调度，且有据可查。

## 6. 风险与硬约束

- **循环不是纯 Python 能替代的。** 契约生成、任务拆解、UNCERTAIN 判定依赖 LLM 判断；P3 是"薄确定性内核 + LLM driver"，不是无人 cron 脚本，不要过度承诺全自动。
- **外部依赖带本机绝对路径。** agent-audits（npm 全局）、codex.exe、`期货分析` ROOT 均硬编码（scheduler_launcher.py:15-18）。必须先做执行器注册表/环境变量，agent-audits 初期包装复用。
- **身份只能做字符串级守卫。** phase1 的 claimed_by 是自由文本，机械上只能阻止"同一 actor 自验收"；A7 AGENT.md 要求的"独立新会话"无法在状态机内证明，只能靠声明 + 审计，未来需要更强认证才能收紧。文档与提示词必须如实说明这条局限。
- **在跑系统安全。** 期货分析的 `.claude/` 与 Windows 任务计划在 P4 割接前一律不动；历史上有 cross_validator 死锁、schtasks 重建的教训，割接逐项目进行。
- **配额 fail-closed 必须保留。** minimax_quota 在查询异常时默认 should_pause=True（minimax_quota.py:72），移植时不得改成 fail-open。
- **SQL 迁移限制。** SQLite `ALTER TABLE ADD COLUMN` 不能同时附带 CHECK；迁移库的新列约束需在应用层兜底，fresh schema 才带完整 CHECK（见 P1 文档第 4 节）。

## 7. 与原 brief 线路 A/B/C 的关系

- 原线路 A（UI）**不在接班关键路径**，降为并行的可视层；但其创建表单、状态时间线会成为 P4 割接后观察内核的主要窗口，继续做但不阻塞 P1/P2。
- 原线路 B（真实执行器）的首要适配器由 claude/cursor **调整为 codex**（老引擎真正主力），并强制吸收重试/日志/失败态。
- 原线路 C（协议加固）上升为 P1，是其余一切的前置。
- 本轮 brief 的三条验收标准仍然有效，但执行排序让位于本路线图。

## 8. 本阶段明确不做（scope boundary）

- 不改 protocol.py / schema.sql 以外的在跑系统，不碰期货分析仓库与任务计划。
- 不在 P1 实现真实 CLI、编排循环或割接（分别属于 P2/P3/P4）。
- 不引入复杂权限系统或认证体系，只做最小可测试的自验收规则。
- 不 push 任何远端、不强制改既有状态转换语义。
