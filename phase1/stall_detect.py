"""停滞检测：把「在途但没产出」的任务退回可认领。

问题（2026-11-24 实测）
------------------------
T-ALERT-008 被深蓝king 认领（claimed）后 **2 小时零产出** —— worktree 里只有
初始检出的文件，没有任何实现、测试或证据。

而 `worklist.sh` 的闸门只看**状态**（claimed = 在途 → WAIT），
**无法区分「在途且正在干活」与「在途但已停摆」**。于是：
* 豆包侧：worklist 显示 WAIT，不会去领新单，也不会重试这一单；
* 唤醒器侧：只扫 dispatched/retry_requested，claimed 的单不在范围内；
* 结果：这一单永久挂死，没有任何机制会推进它。

处置（调度层做，不依赖豆包自觉）
--------------------------------
对**在途（claimed）**的 T-ALERT-* 任务做停滞判定：
  * 超过 STALL_MINUTES（默认 90 分钟）；
  * 且 worktree 内**无任何新文件**（相对 claimed 时刻）；
  * 且 logs/ 下**无 DONE / BLOCKED** 记录（说明既没做完也没如实报障）；
→ 判定为 stalled，把卡片退回 `retry_requested`（可重新认领），并在日志留痕。

**只动状态，不动代码、不删 worktree、不改已交付内容**。
已在终态（delivered/awaiting_review/invalidated…）的单一律不碰。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

BRIDGE = r"C:\Users\32831\wt\bridge-live"
TASKS_DIR = os.path.join(BRIDGE, "tasks")
STALL_MINUTES = int(os.environ.get("DSH_STALL_MINUTES", "90"))
SCOPE_PREFIX = "T-ALERT-"
# 在途状态（claimed 表示豆包已领走但还没交付）
IN_FLIGHT_STATUSES = ("claimed",)
# 这些状态说明已经交付/在审/已完成，不再是「在途」
SETTLED_STATUSES = (
    "awaiting_review", "delivered", "accepted", "completed",
    "invalidated", "withdrawn", "failed", "blocked", "rejected",
)


def _parse_iso(text):
    if not text:
        return None
    t = str(text).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _worktree_has_new_files(worktree: str, since: datetime) -> bool:
    """worktree 内是否有 since 之后新建/修改的文件（排除 .git 与运行时产物）。"""
    if not worktree or not os.path.isdir(worktree):
        return False
    skip_dirs = {".git", "__pycache__", ".pytest_cache", ".agent-audits",
                 "logs", ".idea", "node_modules"}
    cutoff = since.timestamp()
    for root, dirs, files in os.walk(worktree):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                if os.path.getmtime(fp) > cutoff:
                    return True
            except OSError:
                continue
    return False


def _has_done_or_blocked(worktree: str) -> bool:
    """logs/ 下有没有 DONE-* 或 BLOCKED-*（豆包如实报了完成或报障）。"""
    logs = os.path.join(worktree, "logs") if worktree else ""
    if not logs or not os.path.isdir(logs):
        return False
    for fn in os.listdir(logs):
        if fn.startswith("DONE-") or fn.startswith("BLOCKED-"):
            return True
    return False


def check_stalled(verbose=True) -> list:
    """扫描在途任务，返回被判定停滞的 task_id 列表（并已把状态退回）。"""
    now = datetime.now(timezone.utc)
    stalled = []
    if not os.path.isdir(TASKS_DIR):
        return stalled
    for fn in sorted(os.listdir(TASKS_DIR)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(TASKS_DIR, fn)
        try:
            card = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        tid = str(card.get("task_id") or "")
        if not tid.startswith(SCOPE_PREFIX):
            continue
        ex = card.get("execution") or {}
        status = ex.get("status")
        if status not in IN_FLIGHT_STATUSES:
            continue
        claimed_at = _parse_iso(ex.get("claimed_at"))
        if claimed_at is None:
            continue
        age = now - claimed_at
        if age < timedelta(minutes=STALL_MINUTES):
            continue
        worktree = ex.get("worktree") or ""
        if _has_done_or_blocked(worktree):
            continue
        if _worktree_has_new_files(worktree, claimed_at):
            continue
        # 判定停滞 → 退回可认领
        ex["status"] = "retry_requested"
        ex["claimed_at"] = None
        ex["stalled_at"] = now.isoformat()
        ex["stalled_reason"] = (
            "在途超过 %d 分钟且工作区无任何新文件、logs 下无 DONE/BLOCKED，"
            "判定停滞，退回可认领" % STALL_MINUTES)
        card["execution"] = ex
        try:
            json.dump(card, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            stalled.append(tid)
            if verbose:
                print("STALLED %s：在途 %d 分钟无产出 → 退回 retry_requested"
                      % (tid, int(age.total_seconds() // 60)))
        except Exception as exc:
            if verbose:
                print("WARN %s 退回失败：%s" % (tid, exc))
    return stalled


if __name__ == "__main__":
    result = check_stalled()
    print(json.dumps({"stalled": result}, ensure_ascii=False))
