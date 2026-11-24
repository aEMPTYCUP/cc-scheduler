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
from datetime import datetime
from pathlib import Path

PHASE1_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PHASE1_DIR))

CHART = r"C:\Users\32831\wt\dj-chart"

# 执行器分工（用户 2026-11-23 定，2026-11-24 10:20 修订）：
#   * 原规则：MiniMax 2026-11-24 02:06 结束冷却后，新任务一律给 codex；
#   * 修订（江总 2026-11-24 10:20）：codex 走 MiniMax，**token 已耗尽、需再冷却 3+ 小时**，
#     故审核转回 Claude Code（走火山 AgentPlan，目前可用）。
# 实现：用「冷却窗口」表达，不写死单一执行器——窗口内一律 claude，窗口结束后
# 按默认规则回 codex。这样 token 恢复后不需要再改代码。
CODEX_COOLDOWN_UNTIL = datetime(2026, 11, 24, 13, 30)   # 约 3 小时后复查；恢复后改这里即可
DEFAULT_AFTER_COOLDOWN = "codex"


# 2026-11-24 15:35：codex 的 Windows 沙箱缺陷复发 —— 每条命令前都触发
# 沙箱重装（cmd /c echo hi 自检 + codex-windows-sandbox-setup 重跑），
# 空转 25 分钟不产出报告（-o 输出文件 0 字节）。这是**环境缺陷**，不是任务问题。
# 处置：审核执行器暂时回到 claude（火山 AgentPlan 可用），
# 等 codex 沙箱修好再切回。改这一个常量即可。
CODEX_COOLDOWN_UNTIL = datetime(2026, 11, 24, 23, 59)   # 暂定至今日 23:59


def pick_executor(now: datetime | None = None) -> str:
    """选审核执行器：codex 冷却窗口内用 claude，窗口结束后回默认（codex）。"""
    now = now or datetime.now()
    if now < CODEX_COOLDOWN_UNTIL:
        return "claude"
    return DEFAULT_AFTER_COOLDOWN
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





def _pid_alive(pid: int) -> bool:
    """用 tasklist 判断 PID 是否存活（Windows）。查不到时按「活着」保守处理。"""
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=10)
        return str(pid) in (out.stdout or "")
    except Exception:
        return True


# 锁持有者的最大存活时长（秒）。超过即视为**残留锁**（进程被杀/机器休眠/收尾卡死），
# 允许新实例接管 —— 否则一次残留会让审核闭环永久停摆（2026-11-24 实测：
# 21:49 那轮的 claude 已退出，但 python 进程卡在收尾，锁一直被占）。
LOCK_STALE_SECONDS = int(os.environ.get("DSH_REVIEW_LOCK_STALE", "5400"))


def _acquire_lock(lock_file: Path) -> bool:
    """抢单实例锁：写入当前 PID；若已有存活的 PID 持有则放弃。

    残留锁处理：持有者超过 LOCK_STALE_SECONDS（默认 90 分钟）视为已死，
    接管锁继续跑（审核一轮正常要 20-40 分钟，超过 90 分钟基本可判定卡死）。
    """
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        if lock_file.is_file():
            try:
                pid = int(lock_file.read_text(encoding="utf-8").strip() or 0)
            except Exception:
                pid = 0
            if pid and _pid_alive(pid):
                age = time.time() - lock_file.stat().st_mtime
                if age < LOCK_STALE_SECONDS:
                    return False
                log("[LOCK] 持有者 PID %d 已存活 %d 秒（超过 %d），判定为残留锁，接管"
                    % (pid, int(age), LOCK_STALE_SECONDS))
        lock_file.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception:
        return True   # 锁机制失败不阻断审核


def _release_lock(lock_file: Path) -> None:
    try:
        if lock_file.is_file() and lock_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock_file.unlink()
    except Exception:
        pass


MANIFEST_FILE = Path(os.environ.get("DSH_ROUTE_HOME", r"C:\Users\32831\.agent-bridge-route")) / "reviewed.json"


def _reviewed_manifest() -> set:
    """已审任务清单（兼容历史报告：1-A/1-B 的报告标题里没有任务编号）。"""
    try:
        data = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if isinstance(data, dict):
        data = data.get("reviewed") or []
    return set(str(x) for x in data)


def mark_reviewed(task_id: str, report_path: str = "") -> None:
    """登记「该任务已审」，避免重复派审。审核跑完后调用。"""
    try:
        MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {"reviewed": sorted(_reviewed_manifest())}
        if task_id not in data["reviewed"]:
            data["reviewed"].append(task_id)
        data["reviewed"] = sorted(set(data["reviewed"]))
        MANIFEST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass



# 任务 → 审核用仓库（按任务实际交付的代码所在仓库）
_FUTURES_REPO = r"C:\Users\32831\OneDrive\Desktop\期货分析"
_FUTURES_TASKS = ("T-ALERT-009",)   # 阶段5 的路由层在期货仓库


def _review_repo_for(task_id: str):
    """返回 (repo_path, base_branch, review_branch)。

    默认审 dj-chart 的 feature/plan-link；阶段 5 的任务在期货仓库，
    审 master。**不能一律用 dj-chart** —— 否则审核会去审一个不存在的模块。
    """
    if task_id in _FUTURES_TASKS:
        return _FUTURES_REPO, "master", "review/%s" % task_id
    return CHART, "feature/plan-link", "review/%s" % task_id


_IN_FLIGHT_STATUSES = ("CLAIMED", "IN_PROGRESS")


def already_in_flight(task_id: str) -> bool:
    """该任务的审核卡是否正被另一个 review loop 跑着（防并发重复派）。

    2026-11-24 实测教训：手动跑 + 计划任务（每 2 分钟）同时触发时，后到的那次
    atomic_claim 失败（"not READY or already claimed"），其错误处理把任务标成
    FAILED/BLOCKED；先到的那次跑完想 transition SUBMITTED 时状态已非法
    （ValueError: Invalid transition FAILED -> SUBMITTED），报告虽已产出但状态机崩掉。
    """
    import os
    os.environ.setdefault("PHASE1_DB", r"C:\Users\32831\dj-chart-pool.db")
    try:
        import protocol
        local_id = "T-REVIEW-%s" % task_id
        task = protocol.get_task(local_id)
    except Exception:
        return False
    return bool(task) and task.get("status") in _IN_FLIGHT_STATUSES


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



def _contract_for(task_id: str) -> str:
    """按任务编号选契约：阶段2 的 003/004 用阶段2 契约，其余用阶段1 契约。"""
    stage2 = {"T-ALERT-003", "T-ALERT-004", "T-ALERT-005", "T-ALERT-006"}
    name = ("预警中心_阶段2_实现契约.md" if task_id in stage2
            else "预警中心_阶段1_实现契约.md")
    return "docs/" + name


def already_reviewed(task_id: str) -> bool:
    """该任务是否已经有审核产物（避免重复审核）。

    审核产物可能落在不同命名的 worktree（豆包的 wt-<id>-doubao、审核员的
    wt-<id>-CLAUDE 等），所以按**内容**而不是路径判定：凡是提到该 task_id
    的审核报告都算已审。
    """
    # 先查已审清单（兼容标题不含任务编号的历史报告，如 1-A/1-B 那两份）
    if task_id in _reviewed_manifest():
        return True
    wt_root = Path(r"C:\Users\32831\wt")
    # 只认「本任务自己的审核报告」，不能用「正文里提到过该编号」判定：
    # 003 的报告会把 004 作为"范围外"提及，否则 004 会被误判为已审而永不派审。
    # 报告首行格式：# 预警中心 · <task_id> 独立审核报告（...）
    title_patterns = (
        "# 预警中心 · %s 独立审核报告" % task_id,
        "# 预警中心·%s 独立审核报告" % task_id,
    )
    for report in wt_root.glob("wt-*/.agent-audits/alerts-review.md"):
        try:
            head = report.read_text(encoding="utf-8", errors="ignore")[:800]
        except Exception:
            continue
        for pat in title_patterns:
            if pat in head:
                return True
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
    executor = pick_executor()          # 02:06 之后 = codex；建卡时就要按它写，否则池里挂着 claude
    contract = _contract_for(task_id)   # 阶段1 任务用 §11 契约，阶段2 用 §6/§7 契约
    if protocol.get_task(local_id) is None:
        protocol.create_task(
            local_id, "独立审核 %s" % task_id,
            "按契约 %s 逐条审核 %s 的交付，产出 .agent-audits/alerts-review.md。"
            "只审核不补实现；缺失项写成报告，由协调方发返工单。\n"
            "【重要·不要拉真实行情】审核只跑单测与 git 证据："
            "禁止访问行情数据源（data/*.dat 会 404）、禁止跑 plan.run_pipeline 取真实 K 线；"
            "需要真实数据时用注入的假数据。" % (contract, task_id),
            "A7-code-reviewer", "契约对应章节逐条有结论", priority="high",
            allowed_paths=[".agent-audits/alerts-review.md", "alerts/tests/test_review.py"],
            behavioral_check="C:/Users/32831/openmontage/.venv/Scripts/python.exe -m pytest alerts/tests -q",
            executor_id=executor)
        protocol.ready_task(local_id)
    else:
        # 已存在的卡要纠正执行器（历史建卡写死了 claude，02:06 后必须改）
        existing = protocol.get_task(local_id)
        if existing.get("executor_id") != executor:
            protocol.reassign_task(local_id, executor) if hasattr(protocol, "reassign_task") else None
            with protocol.get_db() as conn:
                conn.execute("UPDATE tasks SET executor_id=? WHERE task_id=?",
                             (executor, local_id))
                conn.commit()

    # 审核必须在**独立工作区**：不能在被审方的工作区里写文件，
    # 否则审核产物会污染交付方（正是 T-ALERT-002 首轮被误判的同类问题）。
    # 审核工作区必须落在**该任务代码所在的仓库**：
    # 阶段 5 的 T-ALERT-009 在期货仓库（期货分析），不在 dj-chart。
    # 若一律建在 dj-chart，审核会去审一个不存在的模块 —— 2026-11-24 实测踩到。
    repo_path, base, branch = _review_repo_for(task_id)

    review_wt = r"C:\Users\32831\wt\wt-REVIEW-%s" % task_id
    wt_path = Path(review_wt)
    # 有效性判定：目录在**且**它是该仓库的 git worktree。
    # 只看 is_dir() 会被残留目录骗过（清理脚本删了内容但 .git 文件还在），
    # 于是跳过 worktree add → 审核在一个非仓库目录里跑（2026-11-24 实测）。
    def _is_valid_worktree():
        if not wt_path.is_dir():
            return False
        dotgit = wt_path / ".git"
        if not dotgit.exists():
            return False
        try:
            out = subprocess.run(["git", "-C", str(wt_path), "rev-parse", "--is-inside-work-tree"],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=20)
            return "true" in (out.stdout or "").strip()
        except Exception:
            return False

    if not _is_valid_worktree():
        # 残留目录先清掉再建（worktree 必须建在**该任务代码所在的仓库**里）
        if wt_path.is_dir():
            subprocess.run(["cmd", "/c", "rmdir /s /q " + review_wt], capture_output=True)
        git("worktree", "prune", cwd=repo_path, check=False)
        git("branch", "-D", branch, cwd=repo_path, check=False)
        git("worktree", "add", "-b", branch, review_wt, base, cwd=repo_path)
    task = protocol.get_task(local_id)
    task["worktree_path"] = review_wt
    executor = pick_executor()
    if executor == "codex":
        # 02:06 之后：新任务一律 codex（它走 MiniMax）。
        from codex_adapter import CodexAdapter
        from real_adapters import commit_whitelisted
        adapter = CodexAdapter(
            repo_path, timeout=3600,
            committer=lambda wt, tsk: commit_whitelisted(
                wt, tsk, "task %s: review via codex" % tsk.get("task_id")))
    else:
        adapter = ClaudeSubprocessAdapter(repo_path, timeout=3600)
    summary = adapter.implement(task, executor)
    status = protocol.get_task(local_id)["status"]
    if status in ("REVIEW", "SUBMITTED"):
        # 审核跑完就登记，避免下一轮 review loop 再派一次（历史教训见 already_in_flight）
        mark_reviewed(task_id)
    return {"task_id": task_id, "executor": executor, "summary": summary,
            "status": status}


def main() -> int:
    parser = argparse.ArgumentParser(description="路由队列 → 独立审核闭环")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true", help="只列出会做什么，不真跑审核")
    args = parser.parse_args()

    # 单实例锁：计划任务每 2 分钟触发一次，若上一轮还没跑完（审核要 20-40 分钟），
    # 新一轮会与旧轮并发抢同一任务，导致 atomic_claim 冲突 → 把正在审核的任务
    # 标成 FAILED → 先到者提交时状态非法（2026-11-24 实测事故）。
    lock_file = Path(os.environ.get("DSH_ROUTE_HOME", r"C:\Users\32831\.agent-bridge-route")) / "reviewloop.lock"
    if not _acquire_lock(lock_file):
        log("[LOCK] 另一轮 review loop 正在运行，本轮退出")
        return 0

    def one_round() -> None:
        items = pending_reviews()
        log("[QUEUE] %d 条待审核（T-ALERT-*）" % len(items))
        for item in items:
            task_id = item["task_id"]
            if already_reviewed(task_id):
                log("[SKIP] %s 已有审核产物" % task_id)
                continue
            if already_in_flight(task_id):
                log("[SKIP] %s 正在被另一个 review loop 审核（防并发重复派）" % task_id)
                continue
            if args.dry_run:
                log("[DRY] 将审核 %s（seq=%s）| 执行器=%s" % (
                    task_id, item.get("seq"), pick_executor()))
                continue
            log("[REVIEW] 开始审核 %s" % task_id)
            try:
                result = run_review(item)
                log("[REVIEW] %s → %s" % (task_id, result["summary"]))
            except Exception as exc:
                log("[ERROR] %s 审核失败：%s: %s" % (task_id, type(exc).__name__, exc))

    if args.watch:
        try:
            while True:
                try:
                    one_round()
                except Exception as exc:
                    log("[ERROR] %s: %s" % (type(exc).__name__, exc))
                time.sleep(max(args.interval, 60))
        finally:
            _release_lock(lock_file)
    try:
        one_round()
    finally:
        _release_lock(lock_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
