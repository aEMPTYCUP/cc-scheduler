# 期货分析系统

## Agent Audits 验收规则（最高优先级）

在声称任何编码任务已完成、已修复、正确、已验证、可审查或可合并之前，必须使用 Agent Audits。
如果用户问"你怎么知道？"，只能从当前的 Agent Audits 证据回答：验收标准、证据 ID、命令或产物、审查笔记和 `agent-audits check` 判定。
如果当前没有证据，要么收集证据，要么声明工作尚未验证。

## 调度系统

本项目运行三层自动化任务系统：
- **CC（Claude Code）**：任务规划、优先级调度、契约定义、验收编排、Git 提交决策。不直接执行代码修改。
- **Codex**：仅执行代码修改任务（`C:/Users/32831/.codex/.sandbox-bin/codex.exe`）。
- **acceptance-auditor**：独立验收 Agent，只读，只报告 PASS/FAIL/UNCERTAIN。

调度循环定义在 `.claude/scheduler-prompt.md`，由定时器每 15 分钟唤醒执行一轮。

## 项目结构

- `backend/`：FastAPI 后端（Python）
- `frontend/`：Vue 3 前端
- `hole-insight/`：独立子项目（不被本仓库 git 追踪）
- `.agent-audits/`：验收证据与契约
- `.claude/`：调度 prompt 与验收 Agent 定义
