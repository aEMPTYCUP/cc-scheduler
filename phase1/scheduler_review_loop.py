#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调度闭环：路由队列 → 独立审核 → 合入（只处理 T-ALERT-* 预警中心系列）。

职责边界
--------
* **只认领路由队列里 action=review 且属于 T-ALERT-* 的条目**（其它任务是
  期货旧链路的，不在本闭环内，避免误动停工范围）。
* 审核由 Claude Code 执行（按分工：豆包编程、Claude 审核验收、codex 暂停）。
* 本脚本**不写生产状态**：合入走 git、合入后由调用方决定是否推送；
  责任卡与邮箱状态仍只由 CLI 与调度层写。

用法:
    python -m scheduler_review_loop --once          # 跑一轮
    python -m scheduler_review_loop --watch         # 循环
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PHASE1_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PHASE1_DIR))

CHART = r"C:\Users\32831\wt\dj-chart"
BRIDGE = r"C:\Users\32831\wt\bridge-live"
ROUTE_DIR = Path(os.environ.get("DSH_ROUTE_HOME", r"C:\Users\32831\.agent-bridge-route")) / "route"
TARGET_PREFIX = "T-ALERT-"


def log(msg: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("%s\t%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def git(*args, cwd=CHART, check=True):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check)


def pending_reviews() -> list[dict]:
    """路由队列里尚未处理的 review 条目（同一任务只取最新一条）。"""
    if not ROUTE_DIR.is_dir():
        return []
    latest: dict[str, dict] = {}
    for path in sorted(ROUTE_DIR.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("action") != "review":
            continue
        tid = str(item.get("task_id") or "")
        if not tid.startswith(TARGET_PREFIX):
            continue
        seq = int(item.get("seq") or 0)
        if tid not in latest or seq >= int(latest[tid].get("seq") or 0):
            latest[tid] = item
    return [latest[k] for k in sorted(latest, key=lambda t: int(latest[t].get("seq") or 0))]


def already_reviewed(task_id: str) -> bool:
    """该任务是否已经有审核产物（避免重复审核）。

    审核产物可能落在不同命名的 worktree（豆包的 wt-<id>-doubao、审核员的
    wt-<id>-CLAUDE 等），所以按**内容**而不是路径判定：凡是提到该 task_id
    的审核报告都算已审。
    """
    wt_root = Path(r"C:\Users\32831\wt")
    for report in wt_root.glob("wt-*/.agent-audits/alerts-review.md"):
        try:
            if task_id in report.read_text(encoding="utf-8", errors="ignore"):
                return True
        except Exception:
            continue
    return False


def card_worktree(task_id: str) -> str | None:
    path = Path(BRIDGE) / "tasks" / ("%s.json" % task_id)
    if not path.is_file():
        return None
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return (card.get("execution") or {}).get("worktree")


def run_review(item: dict) -> dict:
    """用 Claude Code 审核该任务的交付（真实执行器）。"""
    task_id = item["task_id"]
    sys.path.insert(0, str(PHASE1_DIR))
    os.environ.setdefault("PHASE1_DB", r"C:\Users\32831\dj-chart-pool.db")
    import protocol  # noqa: E402
    from real_adapters import ClaudeSubprocessAdapter  # noqa: E402

    protocol.ensure_schema()
    with protocol.get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO role_specs (role_id,name,content_hash,content,created_at) "
            "VALUES (?,?,?,?,?)", ("A7-code-reviewer", "A7", "h", "c", protocol.utcnow()))
        conn.commit()

    local_id = "T-REVIEW-%s" % task_id
    if protocol.get_task(local_id) is None:
        protocol.create_task(
            local_id, "独立审核 %s" % task_id,
            "按契约 docs/预警中心_阶段1_实现契约.md §11 逐条审核 %s 的交付，"
            "产出 .agent-audits/alerts-review.md 与 alerts/tests/test_review.py。"
            "只审核不补实现。" % task_id,
            "A7-code-reviewer", "契约 §11 逐条有结论", priority="high",
            allowed_paths=["alerts/tests/test_review.py", ".agent-audits/alerts-review.md"],
            behavioral_check="C:/Users/32831/openmontage/.venv/Scripts/python.exe -m pytest alerts/tests -q",
            executor_id="claude")
        protocol.ready_task(local_id)

    task = protocol.get_task(local_id)
    task["worktree_path"] = card_worktree(task_id) or task.get("worktree_path")
    adapter = ClaudeSubprocessAdapter(CHART, timeout=3600)
    summary = adapter.implement(task, "claude")
    return {"task_id": task_id, "summary": summary,
            "status": protocol.get_task(local_id)["status"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="路由队列 → 独立审核闭环")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true", help="只列出会做什么，不真跑审核")
    args = parser.parse_args()

    def one_round() -> None:
        items = pending_reviews()
        log("[QUEUE] %d 条待审核（T-ALERT-*）" % len(items))
        for item in items:
            task_id = item["task_id"]
            if already_reviewed(task_id):
                log("[SKIP] %s 已有审核产物" % task_id)
                continue
            if args.dry_run:
                log("[DRY] 将审核 %s（seq=%s）" % (task_id, item.get("seq")))
                continue
            log("[REVIEW] 开始审核 %s" % task_id)
            try:
                result = run_review(item)
                log("[REVIEW] %s → %s" % (task_id, result["summary"]))
            except Exception as exc:
                log("[ERROR] %s 审核失败：%s: %s" % (task_id, type(exc).__name__, exc))

    if args.watch:
        while True:
            try:
                one_round()
            except Exception as exc:
                log("[ERROR] %s: %s" % (type(exc).__name__, exc))
            time.sleep(max(args.interval, 60))
    one_round()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
