window.PHASE1_DATA = {
  "generated_at": "2026-11-17T02:39:05.515855+00:00",
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
      "worktree_path": null
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
      "worktree_path": null
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
      "worktree_path": null
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
  ]
};
