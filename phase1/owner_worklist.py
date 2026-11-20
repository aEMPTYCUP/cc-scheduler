# -*- coding: utf-8 -*-
"""只读：某个豆包账号的邮箱工作清单 + 退出闸门判定。

给「豆包账号 + 每小时定时任务」用的一行状态接口。**只读**：只读责任卡、控制台
queue.db（只读 URI）和调度层空闲信号文件，不写、不认领、不交付、不碰 App。

用法（Windows 本机）：
    C:\\Users\\32831\\cc-scheduler\\.venv\\Scripts\\python.exe ^
        C:\\Users\\32831\\cc-scheduler\\phase1\\owner_worklist.py --owner doubao-shenlanking

输出：单行 ASCII JSON（跨 ssh 安全，ensure_ascii=True）。关键字段：

    action : WORK | WAIT | EXIT | HOLD
        WORK = 本账号有待认领任务 -> 继续干活（做完回到本命令重扫）
        WAIT = 无待认领任务，但有在途任务在等独立验收
        EXIT = 已满足退出条件（无新单 + 无在途 + 提交的任务全 PASS + 调度层给了空闲信号）
        HOLD = 条件未齐（缺空闲信号，或有任务挂在人身上没被调度层登记）
    pending / in_flight / parked : 本账号责任卡上的任务 id
    not_passed : 本账号提交过、但还没拿到 PASS 的任务 id
    exit_ok / exit_reason : 退出闸门的判定与理由
    signal : 调度层空闲信号文件的原文摘要

数据源（三方交叉，缺一不可）：
    1. 责任卡 bridge-live/tasks/*.json        -> 执行事实（认领/在途/验收结论）
    2. 控制台 queue.db（只读 URI）             -> 人工侧任务列表（ACCEPTED/DONE = 已验收）
    3. 调度层空闲信号 bridge-live/signals/<owner>.json -> 调度层显式信号
"""
from __future__ import annotations
import os

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_BRIDGE_ROOT = Path(os.environ.get("DSH_BRIDGE_ROOT", r"C:/Users/32831/wt/bridge-live"))
DEFAULT_QUEUE_DB = Path(os.environ.get("DSH_QUEUE_DB", r"C:/Users/32831/cc-scheduler/phase1/.local/queue.db"))
DEFAULT_SIGNAL_DIRNAME = "signals"

# 责任卡状态词表（与 agent_bridge/responsibility.py 保持一致）
CLAIMABLE_STATUSES = {"dispatched", "retry_requested"}
REVIEW_STATUSES = {"awaiting_review", "auditing"}
ACTIVE_STATUSES = {"claimed", "in_progress"}
PARKED_STATUSES = {"needs_human", "blocked"}
LIVE_STATUSES = CLAIMABLE_STATUSES | REVIEW_STATUSES | ACTIVE_STATUSES | PARKED_STATUSES

# 控制台任务列表状态词表
CONSOLE_PASSED = {"ACCEPTED", "DONE"}
CONSOLE_PARKED = {"NEEDS_HUMAN"}


def _read_json(path: Path, errors: List[str]) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        errors.append("%s: %s: %s" % (path.name, type(exc).__name__, exc))
        return None


def read_cards(bridge_root: Path, errors: List[str]) -> Dict[str, Dict[str, Any]]:
    """tid -> 责任卡关键字段。"""
    out: Dict[str, Dict[str, Any]] = {}
    tasks_dir = bridge_root / "tasks"
    if not tasks_dir.is_dir():
        errors.append("tasks dir missing: %s" % tasks_dir)
        return out
    for path in sorted(tasks_dir.glob("*.json")):
        data = _read_json(path, errors)
        if not isinstance(data, dict):
            continue
        execution = data.get("execution") or {}
        audit = data.get("audit") or {}
        tid = str(data.get("task_id") or path.stem)
        out[tid] = {
            "owner": execution.get("owner"),
            "status": execution.get("status"),
            "attempt": execution.get("attempt"),
            "run_id": execution.get("run_id"),
            "worktree": execution.get("worktree"),
            "branch": execution.get("branch"),
            "verdict": audit.get("verdict"),
            "audit_run_id": audit.get("audit_run_id"),
        }
    return out


def read_console(queue_db: Path, owner: str, errors: List[str]) -> Dict[str, str]:
    """claimed_by == owner 的任务 -> 控制台状态。"""
    out: Dict[str, str] = {}
    if not queue_db.is_file():
        errors.append("queue db missing: %s" % queue_db)
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % queue_db, uri=True)
    except sqlite3.Error as exc:
        errors.append("queue db open failed: %s" % exc)
        return out
    try:
        cur = con.execute(
            "SELECT task_id, status FROM tasks WHERE claimed_by = ?", (owner,)
        )
        for task_id, status in cur.fetchall():
            out[str(task_id)] = status
    except sqlite3.Error as exc:
        errors.append("queue db query failed: %s" % exc)
    finally:
        con.close()
    return out


def read_console_owners(queue_db: Path, errors: List[str]) -> Dict[str, Optional[str]]:
    """全部任务的 task_id -> claimed_by（不按 owner 过滤）。

    read_console() 只看 claimed_by == owner 的任务，所以「卡上的 owner 与
    控制台里的 claimed_by 不一致」这种漂移它是看不见的：任务若还挂在旧账号
    名下，新账号的清单里它根本不存在。这个全量视图专门用来暴露这种错配。
    """
    out: Dict[str, Optional[str]] = {}
    if not queue_db.is_file():
        errors.append("queue db missing: %s" % queue_db)
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % queue_db, uri=True)
    except sqlite3.Error as exc:
        errors.append("queue db open failed: %s" % exc)
        return out
    try:
        cur = con.execute("SELECT task_id, claimed_by FROM tasks")
        for task_id, claimed_by in cur.fetchall():
            out[str(task_id)] = claimed_by
    except sqlite3.Error as exc:
        errors.append("queue db query failed: %s" % exc)
    finally:
        con.close()
    return out


def read_signal(signal_dir: Path, owner: str, errors: List[str]) -> Dict[str, Any]:
    path = signal_dir / ("%s.json" % owner)
    data = _read_json(path, errors)
    if not isinstance(data, dict):
        return {"present": False, "idle": False, "parked_on_human": [], "note": None}
    data["present"] = True
    data.setdefault("idle", False)
    data.setdefault("parked_on_human", [])
    return data


def read_dispatch(worktree: Optional[str], tid: str, errors: List[str]) -> Dict[str, Any]:
    """读派工单，只取执行者要用的几个字段。"""
    if not worktree:
        return {}
    path = Path(worktree) / ("DISPATCH-%s.json" % tid)
    data = _read_json(path, errors)
    if not isinstance(data, dict):
        return {}
    instructions = data.get("instructions") or {}
    rework = data.get("rework") or {}
    return {
        "dispatch_doc": str(path),
        "run_id": data.get("run_id"),
        "worktree": data.get("worktree") or worktree,
        "branch": data.get("branch"),
        "allowed_paths": data.get("allowed_paths"),
        "behavioral_check": data.get("behavioral_check") or instructions.get("behavioral_check"),
        "pass_condition": data.get("pass_condition") or instructions.get("pass_condition"),
        "rework_reason": rework.get("reason"),
        "rework_next_attempt": rework.get("next_attempt"),
    }


def collect(owner: str,
            bridge_root: Path = DEFAULT_BRIDGE_ROOT,
            queue_db: Path = DEFAULT_QUEUE_DB,
            signal_dir: Optional[Path] = None) -> Dict[str, Any]:
    """汇总某账号的完整状态视图（纯读）。"""
    errors: List[str] = []
    signal_dir = signal_dir or (bridge_root / DEFAULT_SIGNAL_DIRNAME)

    cards = read_cards(bridge_root, errors)
    console = read_console(queue_db, owner, errors)
    console_owners = read_console_owners(queue_db, errors)
    signal = read_signal(signal_dir, owner, errors)

    owner_cards = {tid: v for tid, v in cards.items() if v.get("owner") == owner}

    pending = sorted(t for t, v in owner_cards.items() if v["status"] in CLAIMABLE_STATUSES)
    # 在途 = 这个账号还有活要干 / 还在等验收。挂在人身上的（needs_human / blocked）
    # 不算在途，否则账号永远退不出去；它们改由「调度层登记 parked_on_human」来兜。
    in_flight = sorted(t for t, v in owner_cards.items()
                       if v["status"] in (CLAIMABLE_STATUSES | REVIEW_STATUSES | ACTIVE_STATUSES))
    review_pending = sorted(t for t, v in owner_cards.items() if v["status"] in REVIEW_STATUSES)
    parked = sorted(t for t, v in owner_cards.items() if v["status"] in PARKED_STATUSES)

    # 提交过但还没拿到 PASS：责任卡侧看 audit.verdict，控制台侧看状态
    not_passed = set()
    for tid, view in owner_cards.items():
        if view.get("verdict") != "PASS":
            not_passed.add(tid)
    for tid, status in console.items():
        if status not in CONSOLE_PASSED:
            not_passed.add(tid)
    not_passed = sorted(not_passed)

    pending_detail = []
    for tid in pending:
        detail = dict(owner_cards.get(tid, {}))
        detail["task_id"] = tid          # 显式带上 task_id，消费方不必再靠下标对齐
        detail.update(read_dispatch(detail.get("worktree"), tid, errors))
        worktree = detail.get("worktree")
        rework_note = None
        if worktree:
            candidate = Path(worktree) / "logs" / ("REWORK-%s.txt" % tid)
            rework_note = str(candidate) if candidate.is_file() else None
        detail["rework_note_path"] = rework_note
        pending_detail.append(detail)

    # 闸门：a 无待认领 b 无在途 c 提交过的全 PASS（挂在人身上的由调度层登记）d 调度层空闲信号
    acked = set(signal.get("parked_on_human") or [])
    blockers: List[str] = []
    if pending:
        blockers.append("pending=%s" % ",".join(pending))
    if in_flight:
        blockers.append("in_flight=%s" % ",".join(in_flight))
    if not signal.get("present"):
        blockers.append("no_signal")
    elif not signal.get("idle"):
        blockers.append("signal_idle_false")
    unacked = sorted(set(not_passed) - acked)
    if unacked:
        blockers.append("not_passed_not_acked=%s" % ",".join(unacked))
    exit_ok = not blockers

    if pending:
        action = "WORK"
    elif in_flight:
        action = "WAIT"
    elif exit_ok:
        action = "EXIT"
    else:
        action = "HOLD"

    anomalies = []
    for tid in sorted(set(owner_cards) | set(console)):
        card_status = (owner_cards.get(tid) or {}).get("status")
        console_status = console.get(tid)
        if card_status and console_status:
            card_passed = card_status not in LIVE_STATUSES
            console_passed = console_status in CONSOLE_PASSED
            if card_passed != console_passed:
                anomalies.append({
                    "tid": tid,
                    "card_status": card_status,
                    "console_status": console_status,
                })
    # 归属漂移：卡上的 owner 是本账号，但控制台里还挂在别的账号名下。
    # read_console 按 claimed_by 过滤，看不见这种错配，所以这里用全量视图比对。
    # 控制台 claimed_by 为空不算错配：卡处于 dispatched 时本来就还没人认领，
    # 等新账号的 claimed 事件到了，控制台才会把它记到新账号名下。
    for tid in sorted(owner_cards):
        console_owner = console_owners.get(tid, "__absent__")
        if console_owner == "__absent__":
            anomalies.append({
                "tid": tid,
                "kind": "not_in_console",
                "card_owner": owner,
                "console_owner": None,
                "card_status": owner_cards[tid].get("status"),
            })
        elif console_owner not in (None, owner):
            anomalies.append({
                "tid": tid,
                "kind": "owner_mismatch",
                "card_owner": owner,
                "console_owner": console_owner,
                "card_status": owner_cards[tid].get("status"),
            })

    return {
        "schema_version": 1,
        "owner": owner,
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "exit_ok": exit_ok,
        "exit_reason": "ok" if exit_ok else "; ".join(blockers),
        "pending": pending,
        "pending_detail": pending_detail,
        "in_flight": in_flight,
        "review_pending": review_pending,
        "parked": parked,
        "not_passed": not_passed,
        "console_tasks": dict(sorted(console.items())),
        "card_tasks": {t: owner_cards[t] for t in sorted(owner_cards)},
        "anomalies": anomalies,
        "signal": {
            "present": bool(signal.get("present")),
            "idle": bool(signal.get("idle")),
            "issued_at_utc": signal.get("issued_at_utc"),
            "note": signal.get("note"),
            "parked_on_human": sorted(acked),
        },
        "errors": errors,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only per-owner mailbox worklist.")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--bridge-root", default=str(DEFAULT_BRIDGE_ROOT))
    parser.add_argument("--queue-db", default=str(DEFAULT_QUEUE_DB))
    parser.add_argument("--signal-dir", default=None)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    signal_dir = Path(args.signal_dir) if args.signal_dir else None
    result = collect(args.owner, Path(args.bridge_root), Path(args.queue_db), signal_dir)
    if args.pretty:
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
