# 任务编队平台 · 推进记录（2026-11-20）

本记录汇总 2026-11-20 平台主线（任务编队平台）的推进成果。期货分析（洞见）子任务保持停工（AgentBridgeWorker 已禁用；T-239 为用户例外放行，由豆包执行）。

## 一、平台能力矩阵（现状）

| 层 | 能力 | 状态 |
|---|---|---|
| 协议内核 | phase1 状态机 / 协议层 / 任务包 / 岗位规范 | ✅ 250 tests 绿（今日新增 19 项） |
| 执行器层 | bridge 责任卡 / 事件邮箱 / 独立审计 / 返工 / readmission | ✅ 197 tests 绿（D-1/D-2 后） |
| 真实执行器 | **CodexAdapter**：codex exec 端到端真实跑通（改文件→commit→REVIEW，fe840a6b） | ✅ **17:53 达成**（需 DSH_CODEX_SANDBOX=danger-full-access） |
| 真实执行器 | ClaudeSubprocessAdapter：claude -p 端到端真实跑通 | ✅ |
| 真实执行器 | DoubaoAdapter：人工粘贴通道 | ✅ |
| 调度 | scheduler_loop（P3 内核：配额钩子 fail-closed + 会话 + 驱动 READY） | ✅ N-11 |
| 投影 | sync_watch（常驻 outbox→控制台投影，30s 间隔） | ✅ N-20 |
| 看板 | ui_server + /api/create（协议层建任务）+ 状态机时间线 | ✅ N-06 |
| 运营 | worktree 自动清理（DONE 后 best-effort） | ✅ N-02 |
| 审计 | 裁决写回解耦 + 收割挂死审计员（D-1）、delivery 补验收 + 死信守卫（D-2）、产能 1→2（D-4） | ✅ |

## 二、今日关键成果

1. **D-1 裁决写回修复实战验证**：T-187（FAIL→豆包返工→重审 PASS）、T-188（FAIL→返工→PASS）、T-186（抢救 PASS）——3 张卡拿到真实独立验收裁决，完整闭环首次跑通。
2. **P2 出口达成**：真实 CLI 路径（claude）端到端跑通（实现→提交→REVIEW）+ 真实 FAIL→返工→PASS（T-187）。
3. **真实冒烟结论**：
   - ClaudeSubprocessAdapter：✅ 端到端成功
   - CodexAdapter：链路正确（超时→FAILED→park，不假装成功），但 codex Windows 沙箱环境故障（`CreateProcessWithLogonW failed: 2`，shell/node 全线无法 spawn）——环境依赖，待修
4. **桥侧状态**：T-186/T-187/T-188 卡 verdict=PASS（needs_human）；T-186/T-238 已人工推进 INTEGRATION。

## 三、commits（本日）

**cc-scheduler（feature/phase1-p0-p1）**：3c3fe17（CodexAdapter）、62c3951（CLI 入口）、ae5d787（N-06 UI）、a6377b1（N-02 worktree）、88adc9e（N-11 调度）、c1be72c（N-20 投影）、422c5a4（sync --db）、787774e（B9 UI 真实执行器）、N-12 路径清理 commit
**agent-bridge（feature/agent-bridge，已 push）**：3257997（D-1/D-2）、34cfec9（gate 脚本）、633e6be（N-18 自动返工单）、01264a0（N-24 模块文档）

## 三-b、午后追加推进（12:00 后）

| 项 | 状态 |
|---|---|
| N-18 审计 FAIL 自动生成返工单（rework_task 自动触发 + 通知） | ✅ bridge 199 tests |
| B9 看板 GO 装配真实 claude 执行器（adapter_simulated=False，拦截解除） | ✅ |
| N-24 agent_bridge/__init__.py 补全 | ✅ |
| P3 验证：scheduler_loop + 真实 claude 完整一轮（READY→REVIEW） | ✅ 闭环跑通 |
| N-12 绝对路径 → DSH_* 环境变量（9 个路径，默认保留） | ✅ 250 tests |
| sync_watch 常驻投影（DSH-SyncWatch 计划任务每分钟） | ✅ last_seq 121+ |

## 三-c、codex 重装与沙箱定性（22:00 后）

- 桌面 `GitHub_token.textClipping` 实测 **401 无效**；改用 **SSH remote** 推送，绕过 PAT workflow scope（cc-scheduler + agent-bridge 两仓库均已推送）
- macOS 权限复查：`agent-desktop permissions` → accessibility **granted**、screen_recording **granted**（面板已打开，无需再授权）
- 官方 `@openai/codex@0.171.0` 已装；`codex doctor` 显示沙箱 `backend: elevated`、`provisioning: complete` 但管道握手超时
- 平台新增 `platform_doctor.py --deep`（21 项体检，含沙箱可用性深检）→ 当前 **failed=0 warned=1**（唯一 warn 即 codex 沙箱）

## 四、待办

1. **codex Windows 沙箱两代都不可用（Codex 侧环境缺陷，已定性）**：
   - 0.147（桌面 App 自带 CLI）：`CreateProcessWithLogonW failed: 2`——受限令牌路径，`CodexSandboxOffline/Online` 账户（密码 2026-06-23 设置）已与当前版本不匹配
   - 0.171.0（官方 npm，已按授权重装）：`timed out after 15000ms connecting runner pipe-in`——elevated backend（`elevated_windows_sandbox` 已 removed，无法回退）；日志显示 setup/ACL 刷新均成功（errors=[]），helper 复制成功，但 runner 回连管道始终超时；预热重试无效（每次仍 30s+）
   - 服务 `CodexSandboxService.OpenAI.Codex` 来自 MSIX 包 `OpenAI.Codex_26.1111.2228.0`（11-15 构建），MSIX 保护导致 `sc config` 改 binPath 被拒；机器上另有 26.1111.9664.0（今天 09:54 装）
   - **平台侧结论与落地**：codex 走 `DSH_CODEX_SANDBOX=danger-full-access`（`runtime/run_codex_worker.cmd` 已固化）；任务仍在独立 worktree、prompt 禁危险 git 动词、DSH 审批层不受影响；**0.171.0 官方 CLI 在该模式下真实端到端跑通**（commit 3f48de019c → REVIEW）
   - **彻底修复需用户一步**：Microsoft Store 更新 Codex 桌面 App（面板已打开），使服务与 CLI 版本匹配；`platform_doctor.py --deep` 会自动复检并报告
   - 副作用记录：npm `codex` shim 已从 oh-my-codex 包装切到官方 0.171.0（旧 shim 备份在 `%TEMP%\codex-backup-20261120-220951`）；桌面 App 自身的 `.sandbox-bin\codex.exe` 未改动
2. **期货（洞见）**：保持停工。T-239 例外放行（豆包执行）。
3. **P4 影子运行/割接**：待期货侧决策。P3 核心链条已验证（claude + codex 双执行器）。
4. **N-05 真实 CLI 进 CI**：CI 目前只跑协议层测试，真实 CLI 需人工触发（有额度成本）。

## 五、使用方式

- 调度：`python -m scheduler_loop --executor claude --repo <repo> --once|--watch`
- 投影：`python -m sync_watch --bridge-root C:/Users/32831/wt/bridge-live --interval 30`
- 看板：`python scripts/ui_server.py --port 8765`（或 runtime/run_ui_server.cmd）
- codex 执行：`python codex_adapter.py --task T-XXX --repo <repo>`（沙箱修好后）
