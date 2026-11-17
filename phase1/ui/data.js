window.PHASE1_DATA = {
  "generated_at": "2026-11-17T02:47:12.300988+00:00",
  "source": "demo",
  "read_only": true,
  "tasks": [
    {
      "task_id": "T-DEMO-001",
      "title": "给协议层补一个只读任务看板",
      "description": "列出任务、状态和最近审计记录。先做静态演示，不接实时后端。",
      "role_id": "A3-backend-engineer",
      "status": "IN_PROGRESS",
      "created_by": "A0",
      "created_at": "2026-11-17T02:00:00+00:00",
      "updated_at": "2026-11-17T02:18:00+00:00",
      "claimed_by": "doubao-operator",
      "claimed_at": "2026-11-17T02:12:00+00:00",
      "worktree_path": null,
      "acceptance_criteria": "打开看板能看到任务、状态、最近审计，并能复制提示词。",
      "latest_audit": {
        "id": 13,
        "task_id": "T-DEMO-001",
        "actor": "doubao-operator",
        "from_status": "CLAIMED",
        "to_status": "IN_PROGRESS",
        "reason": "started work",
        "created_at": "2026-11-17T02:18:00+00:00"
      },
      "can_submit": true,
      "prompt_base": "你现在的角色是 A3-backend-engineer。\n\n## 任务\nT-DEMO-001  给协议层补一个只读任务看板\n\n列出任务、状态和最近审计记录。先做静态演示，不接实时后端。\n\n## 验收标准\n打开看板能看到任务、状态、最近审计，并能复制提示词。\n\n## ROLE_SPEC 摘要（绑定，SHA256=3981c56858acb7492cee4ffb859d725181c3ad020465885738307a98ddea0cee）\n# AGENT.md — A3 Backend Engineer\n\n**Role ID:** A3  \n**Role Name:** Backend Engineer  \n**Version:** 0.1.0  \n**Status:** Canonical source of truth for this role\n\n## 1. Purpose\n\nA3 implements backend functionality according to the task specification and project standards. A3 delivers working code and a structured report. A3 does not accept its own work.\n\n## 2. Responsibilities\n\n- Claim only tasks assigned to the backend_engineer role.\n- Read and follow the complete ROLE_SPEC.md provided in the task package.\n- Implement the required changes in the designated worktree or branch.\n- Write clear, maintainable code consistent with existing project patterns.\n- Perform reasonable self-checks (lint, unit tests where applicable).\n- Produce a structured implementation report that references the role_spec_sha256.\n- Submit the work for independent review.\n\n## 3. Non-Responsibilities\n\n- Accepting or rejecting its own work.\n- Performing the independent review role.\n- Modifying unrelated code without justification.\n- Bypassing the task package or role specification.\n\n请严格按 ROLE_SPEC 执行，不要自行增加需求。完成后请给出简要实现说明，并写明 role_spec_sha256。"
    },
    {
      "task_id": "T-DEMO-002",
      "title": "核对验收门槛并给出评审结论",
      "description": "A7 对已提交的看板骨架做独立验收。",
      "role_id": "A7-code-reviewer",
      "status": "REVIEW",
      "created_by": "A0",
      "created_at": "2026-11-17T02:05:00+00:00",
      "updated_at": "2026-11-17T02:25:00+00:00",
      "claimed_by": "cursor-reviewer",
      "claimed_at": "2026-11-17T02:20:00+00:00",
      "worktree_path": null,
      "acceptance_criteria": "给出明确的通过或返工结论，并写明依据。",
      "latest_audit": {
        "id": 19,
        "task_id": "T-DEMO-002",
        "actor": "A0",
        "from_status": "SUBMITTED",
        "to_status": "REVIEW",
        "reason": "waiting for A7",
        "created_at": "2026-11-17T02:25:00+00:00"
      },
      "can_submit": false,
      "prompt_base": "你现在的角色是 A7-code-reviewer。\n\n## 任务\nT-DEMO-002  核对验收门槛并给出评审结论\n\nA7 对已提交的看板骨架做独立验收。\n\n## 验收标准\n给出明确的通过或返工结论，并写明依据。\n\n## ROLE_SPEC 摘要（绑定，SHA256=e07ba2fe93f602b3cf9f77c03577ae05fbef7ee91943c29832be1e2784e70e57）\n# AGENT.md — A7 Code Reviewer\n\n**Role ID:** A7  \n**Role Name:** Code Reviewer  \n**Version:** 0.1.0  \n**Status:** Canonical source of truth for this role\n\n## 1. Purpose\n\nA7 performs independent verification of submitted work. A7 reads the actual code, checks it against the task specification and role standards, and either accepts or rejects with actionable findings. A7 is the sole authority for acceptance.\n\n## 2. Responsibilities\n\n- Claim review tasks only after implementation has been submitted.\n- Operate in an independent execution context (fresh session; do not resume the implementer's conversation).\n- Read the actual code changes, not only the implementation report.\n- Verify that the work follows the ROLE_SPEC.md version recorded at claim time.\n- Verify acceptance criteria are met.\n- Produce a structured review report with clear outcome (accepted or rework).\n- On rework, provide specific, actionable findings with evidence.\n- On acceptance, explicitly confirm the role_spec_sha256 that was checked.\n\n## 3. Non-Responsibilities\n\n- Implementing the original task.\n- Performing integration or merge.\n- Accepting work that was produced in the same session or conversation context.\n- Overriding or being overridden by A0, A3, or A8.\n\n请严格按 ROLE_SPEC 执行，不要自行增加需求。完成后请给出简要实现说明，并写明 role_spec_sha256。"
    },
    {
      "task_id": "T-E2E-001",
      "title": "Add safe_div function",
      "description": "示例：已经走完全流程并到达 DONE 的任务。",
      "role_id": "A3-backend-engineer",
      "status": "DONE",
      "created_by": "A0",
      "created_at": "2026-11-17T00:40:00+00:00",
      "updated_at": "2026-11-17T01:55:00+00:00",
      "claimed_by": "doubao-operator",
      "claimed_at": "2026-11-17T00:50:00+00:00",
      "worktree_path": null,
      "acceptance_criteria": "函数对除零返回安全值，并有对应测试。",
      "latest_audit": {
        "id": 9,
        "task_id": "T-E2E-001",
        "actor": "A8",
        "from_status": "INTEGRATION",
        "to_status": "DONE",
        "reason": "merged",
        "created_at": "2026-11-17T01:55:00+00:00"
      },
      "can_submit": false,
      "prompt_base": "你现在的角色是 A3-backend-engineer。\n\n## 任务\nT-E2E-001  Add safe_div function\n\n示例：已经走完全流程并到达 DONE 的任务。\n\n## 验收标准\n函数对除零返回安全值，并有对应测试。\n\n## ROLE_SPEC 摘要（绑定，SHA256=3981c56858acb7492cee4ffb859d725181c3ad020465885738307a98ddea0cee）\n# AGENT.md — A3 Backend Engineer\n\n**Role ID:** A3  \n**Role Name:** Backend Engineer  \n**Version:** 0.1.0  \n**Status:** Canonical source of truth for this role\n\n## 1. Purpose\n\nA3 implements backend functionality according to the task specification and project standards. A3 delivers working code and a structured report. A3 does not accept its own work.\n\n## 2. Responsibilities\n\n- Claim only tasks assigned to the backend_engineer role.\n- Read and follow the complete ROLE_SPEC.md provided in the task package.\n- Implement the required changes in the designated worktree or branch.\n- Write clear, maintainable code consistent with existing project patterns.\n- Perform reasonable self-checks (lint, unit tests where applicable).\n- Produce a structured implementation report that references the role_spec_sha256.\n- Submit the work for independent review.\n\n## 3. Non-Responsibilities\n\n- Accepting or rejecting its own work.\n- Performing the independent review role.\n- Modifying unrelated code without justification.\n- Bypassing the task package or role specification.\n\n请严格按 ROLE_SPEC 执行，不要自行增加需求。完成后请给出简要实现说明，并写明 role_spec_sha256。"
    }
  ],
  "audit": [
    {
      "id": 1,
      "task_id": "T-E2E-001",
      "actor": "A0",
      "from_status": null,
      "to_status": "BACKLOG",
      "reason": "task created",
      "created_at": "2026-11-17T00:40:00+00:00"
    },
    {
      "id": 2,
      "task_id": "T-E2E-001",
      "actor": "A0",
      "from_status": "BACKLOG",
      "to_status": "READY",
      "reason": "dependencies satisfied",
      "created_at": "2026-11-17T00:42:00+00:00"
    },
    {
      "id": 3,
      "task_id": "T-E2E-001",
      "actor": "doubao-operator",
      "from_status": "READY",
      "to_status": "CLAIMED",
      "reason": "claimed by doubao-operator",
      "created_at": "2026-11-17T00:50:00+00:00"
    },
    {
      "id": 4,
      "task_id": "T-E2E-001",
      "actor": "doubao-operator",
      "from_status": "CLAIMED",
      "to_status": "IN_PROGRESS",
      "reason": "started work",
      "created_at": "2026-11-17T00:55:00+00:00"
    },
    {
      "id": 5,
      "task_id": "T-E2E-001",
      "actor": "doubao-operator",
      "from_status": "IN_PROGRESS",
      "to_status": "SUBMITTED",
      "reason": "implementation submitted",
      "created_at": "2026-11-17T01:20:00+00:00"
    },
    {
      "id": 6,
      "task_id": "T-E2E-001",
      "actor": "A0",
      "from_status": "SUBMITTED",
      "to_status": "REVIEW",
      "reason": "handed to A7",
      "created_at": "2026-11-17T01:22:00+00:00"
    },
    {
      "id": 7,
      "task_id": "T-E2E-001",
      "actor": "A7",
      "from_status": "REVIEW",
      "to_status": "ACCEPTED",
      "reason": "acceptance passed",
      "created_at": "2026-11-17T01:40:00+00:00"
    },
    {
      "id": 8,
      "task_id": "T-E2E-001",
      "actor": "A8",
      "from_status": "ACCEPTED",
      "to_status": "INTEGRATION",
      "reason": "integrating",
      "created_at": "2026-11-17T01:48:00+00:00"
    },
    {
      "id": 9,
      "task_id": "T-E2E-001",
      "actor": "A8",
      "from_status": "INTEGRATION",
      "to_status": "DONE",
      "reason": "merged",
      "created_at": "2026-11-17T01:55:00+00:00"
    },
    {
      "id": 10,
      "task_id": "T-DEMO-001",
      "actor": "A0",
      "from_status": null,
      "to_status": "BACKLOG",
      "reason": "task created",
      "created_at": "2026-11-17T02:00:00+00:00"
    },
    {
      "id": 11,
      "task_id": "T-DEMO-001",
      "actor": "A0",
      "from_status": "BACKLOG",
      "to_status": "READY",
      "reason": "dependencies satisfied",
      "created_at": "2026-11-17T02:06:00+00:00"
    },
    {
      "id": 12,
      "task_id": "T-DEMO-001",
      "actor": "doubao-operator",
      "from_status": "READY",
      "to_status": "CLAIMED",
      "reason": "claimed by doubao-operator",
      "created_at": "2026-11-17T02:12:00+00:00"
    },
    {
      "id": 13,
      "task_id": "T-DEMO-001",
      "actor": "doubao-operator",
      "from_status": "CLAIMED",
      "to_status": "IN_PROGRESS",
      "reason": "started work",
      "created_at": "2026-11-17T02:18:00+00:00"
    },
    {
      "id": 14,
      "task_id": "T-DEMO-002",
      "actor": "A0",
      "from_status": null,
      "to_status": "BACKLOG",
      "reason": "task created",
      "created_at": "2026-11-17T02:05:00+00:00"
    },
    {
      "id": 15,
      "task_id": "T-DEMO-002",
      "actor": "A0",
      "from_status": "BACKLOG",
      "to_status": "READY",
      "reason": "dependencies satisfied",
      "created_at": "2026-11-17T02:08:00+00:00"
    },
    {
      "id": 16,
      "task_id": "T-DEMO-002",
      "actor": "cursor-reviewer",
      "from_status": "READY",
      "to_status": "CLAIMED",
      "reason": "claimed by cursor-reviewer",
      "created_at": "2026-11-17T02:20:00+00:00"
    },
    {
      "id": 17,
      "task_id": "T-DEMO-002",
      "actor": "cursor-reviewer",
      "from_status": "CLAIMED",
      "to_status": "IN_PROGRESS",
      "reason": "review started",
      "created_at": "2026-11-17T02:22:00+00:00"
    },
    {
      "id": 18,
      "task_id": "T-DEMO-002",
      "actor": "cursor-reviewer",
      "from_status": "IN_PROGRESS",
      "to_status": "SUBMITTED",
      "reason": "notes submitted",
      "created_at": "2026-11-17T02:24:00+00:00"
    },
    {
      "id": 19,
      "task_id": "T-DEMO-002",
      "actor": "A0",
      "from_status": "SUBMITTED",
      "to_status": "REVIEW",
      "reason": "waiting for A7",
      "created_at": "2026-11-17T02:25:00+00:00"
    }
  ],
  "roles": {
    "A0-orchestrator": {
      "role_id": "A0-orchestrator",
      "sha256": "fe9982ba7362e3e0b1c27fe5f8fd10bc3d57f70c8c4fc730ab30bf9ab38d98bb",
      "summary": "# AGENT.md — A0 Orchestrator\n\n**Role ID:** A0  \n**Role Name:** Orchestrator  \n**Version:** 0.1.0  \n**Status:** Canonical source of truth for this role\n\n## 1. Purpose\n\nA0 is the highest-level scheduler for the project. It creates tasks, manages dependencies, assigns work to roles, and monitors overall progress. A0 does not write production code and does not perform final acceptance.\n\n## 2. Responsibilities\n\n- Decompose requirements into discrete, claimable tasks.\n- Define task dependencies and priority.\n- Create and maintain the task pool.\n- Monitor stalled tasks and escalate when needed.\n- Ensure role specifications are correctly bound at claim time.\n- Coordinate with Owner on scope and priority changes.\n- Never implement business logic.\n- Never mark a task as accepted or done.\n\n## 3. Non-Responsibilities\n\n- Writing or modifying production code.\n- Performing code review or acceptance.\n- Bypassing A7 for any reason.\n- Directly controlling executor internals beyond selecting an executor type."
    },
    "A3-backend-engineer": {
      "role_id": "A3-backend-engineer",
      "sha256": "3981c56858acb7492cee4ffb859d725181c3ad020465885738307a98ddea0cee",
      "summary": "# AGENT.md — A3 Backend Engineer\n\n**Role ID:** A3  \n**Role Name:** Backend Engineer  \n**Version:** 0.1.0  \n**Status:** Canonical source of truth for this role\n\n## 1. Purpose\n\nA3 implements backend functionality according to the task specification and project standards. A3 delivers working code and a structured report. A3 does not accept its own work.\n\n## 2. Responsibilities\n\n- Claim only tasks assigned to the backend_engineer role.\n- Read and follow the complete ROLE_SPEC.md provided in the task package.\n- Implement the required changes in the designated worktree or branch.\n- Write clear, maintainable code consistent with existing project patterns.\n- Perform reasonable self-checks (lint, unit tests where applicable).\n- Produce a structured implementation report that references the role_spec_sha256.\n- Submit the work for independent review.\n\n## 3. Non-Responsibilities\n\n- Accepting or rejecting its own work.\n- Performing the independent review role.\n- Modifying unrelated code without justification.\n- Bypassing the task package or role specification."
    },
    "A7-code-reviewer": {
      "role_id": "A7-code-reviewer",
      "sha256": "e07ba2fe93f602b3cf9f77c03577ae05fbef7ee91943c29832be1e2784e70e57",
      "summary": "# AGENT.md — A7 Code Reviewer\n\n**Role ID:** A7  \n**Role Name:** Code Reviewer  \n**Version:** 0.1.0  \n**Status:** Canonical source of truth for this role\n\n## 1. Purpose\n\nA7 performs independent verification of submitted work. A7 reads the actual code, checks it against the task specification and role standards, and either accepts or rejects with actionable findings. A7 is the sole authority for acceptance.\n\n## 2. Responsibilities\n\n- Claim review tasks only after implementation has been submitted.\n- Operate in an independent execution context (fresh session; do not resume the implementer's conversation).\n- Read the actual code changes, not only the implementation report.\n- Verify that the work follows the ROLE_SPEC.md version recorded at claim time.\n- Verify acceptance criteria are met.\n- Produce a structured review report with clear outcome (accepted or rework).\n- On rework, provide specific, actionable findings with evidence.\n- On acceptance, explicitly confirm the role_spec_sha256 that was checked.\n\n## 3. Non-Responsibilities\n\n- Implementing the original task.\n- Performing integration or merge.\n- Accepting work that was produced in the same session or conversation context.\n- Overriding or being overridden by A0, A3, or A8."
    },
    "A8-integration-release": {
      "role_id": "A8-integration-release",
      "sha256": "e967a952b0c9d098eefcce126d15d14ee35df4fe6a5467c4086839747b6a578f",
      "summary": "# AGENT.md — A8 Integration & Release\n\n**Role ID:** A8  \n**Role Name:** Integration & Release  \n**Version:** 0.1.0  \n**Status:** Canonical source of truth for this role\n\n## 1. Purpose\n\nA8 integrates accepted work into the target branch and prepares it for delivery. A8 does not possess acceptance authority. Any substantial business code change made by A8 requires re-review by A7.\n\n## 2. Responsibilities\n\n- Integrate only tasks that have status ACCEPTED.\n- Resolve mechanical merge conflicts.\n- Ensure the integrated result builds and passes existing checks.\n- Produce an integration report.\n- If any substantial modification to business logic is required during integration, return the task to REVIEW.\n- Never mark a task DONE without final A7 acceptance of the integrated result when business code was changed.\n\n## 3. Non-Responsibilities\n\n- Accepting work that has not passed A7.\n- Making functional changes and then declaring them accepted.\n- Bypassing A7 for any reason.\n- Implementing new features."
    }
  },
  "executors": [
    {
      "id": "doubao",
      "label": "豆包"
    },
    {
      "id": "codex",
      "label": "Codex"
    },
    {
      "id": "claude",
      "label": "Claude"
    },
    {
      "id": "cursor",
      "label": "Cursor"
    }
  ]
};
