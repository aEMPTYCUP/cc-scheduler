#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""邮箱事件路由守护：把桥接事件按规则分派到对应的执行通道。

为什么需要它
------------
豆包在 App 里 deliver / block 之后，事件会落到 agent-bridge 的 outbox，但**没有
任何东西会把它推给协调方（DSH）**：协调方只能主动轮询。这条链路的缺口是
「豆包交付了 → 通知我」，本守护补上它。

为什么用规则而不是本地 7B 推理
------------------------------
事件到动作的映射是**确定性的**（delivered → 触发独立验收；blocked → 触发返工单
准备；claimed → 记账不打扰）。用 7B 去猜一个本来就没有歧义的映射，只会引入
不确定性；本地 7B（Mac Mini Qwen2.5-7B @ 127.0.0.1:8001）留给冻结裁定第 12 条
的另一个场景——**预警中心内的数据查询路由**（问什么 → 7B 匹配数据源），那个场景
才真正需要语义理解。

行为（全部只读 + 通知，不改任何状态）
--------------------------------------
* 只读 `wt/bridge-live/notifications/outbox.jsonl`，按 seq 增量消费；
* 按 kind 映射到动作，写入 `~/.agent-bridge/route/` 下的待办队列文件；
* 打印一行 JSON 摘要到 stdout 与 `route.log`；
* **不写责任卡、不写邮箱、不改生产状态**——状态只由 CLI 和调度层写。

用法:
    python3 ~/.agent-bridge/route_events.py --once
    python3 ~/.agent-bridge/route_events.py --watch --interval 60
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

BRIDGE_ROOT = Path(os.environ.get("DSH_BRIDGE_ROOT", r"C:/Users/32831/wt/bridge-live"))
# 部署在 Windows（事件源同机）：队列与状态写到 C:/Users/32831/.agent-bridge-route
AB_DIR = Path(os.environ.get("DSH_ROUTE_HOME", r"C:/Users/32831/.agent-bridge-route"))
QUEUE_DIR = AB_DIR / "route"
STATE_FILE = AB_DIR / ".route_seq"
LOG_FILE = AB_DIR / "route.log"

# 事件 kind → 路由动作。确定性映射，不做语义猜测。
ROUTES = {
    "delivered": "review",        # 豆包交付 → 需要独立验收（Claude Code）
    "blocked": "rework",          # 豆包阻塞 → 需要准备返工单
    "claimed": "noop",            # 认领只是进度，不打扰
    "dispatched": "noop",         # 派工只是进度
    "rework_requested": "rework",
    "needs_human": "human",       # 等待人工判断
    "completed": "noop",
}


def log(msg: str) -> None:
    line = "%s\t%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    # Windows 控制台默认 GBK，直接 print 中文会乱码；强制 UTF-8 输出。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(line, flush=True)


def last_seq() -> int:
    try:
        return int(STATE_FILE.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def set_last_seq(seq: int) -> None:
    try:
        STATE_FILE.write_text(str(seq), encoding="utf-8")
    except Exception:
        pass


def read_events() -> list[dict]:
    path = BRIDGE_ROOT / "notifications" / "outbox.jsonl"
    if not path.is_file():
        return []
    events = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return events


def enqueue(action: str, event: dict) -> Path:
    """把一条待处理事件写进本地路由队列（协调方据此派活）。"""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    task_id = event.get("task_id") or "unknown"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = QUEUE_DIR / ("%s-%s-%s.json" % (action, task_id, stamp))
    payload = {
        "action": action,
        "seq": event.get("seq"),
        "task_id": task_id,
        "kind": event.get("kind"),
        "owner": event.get("owner"),
        "card_status": event.get("card_status"),
        "ts": event.get("ts"),
        "detail": event.get("detail") or {},
        "routed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def one_round() -> dict:
    events = read_events()
    if not events:
        log("[IDLE] 无事件文件")
        return {"ok": True, "routed": 0}
    cursor = last_seq()
    fresh = [e for e in events if int(e.get("seq") or 0) > cursor]
    if not fresh:
        log("[IDLE] 无新事件（cursor=%s）" % cursor)
        return {"ok": True, "routed": 0}

    routed = []
    for event in fresh:
        kind = event.get("kind") or ""
        action = ROUTES.get(kind, "noop")
        seq = int(event.get("seq") or 0)
        if action == "noop":
            continue
        path = enqueue(action, event)
        routed.append({"seq": seq, "action": action, "task_id": event.get("task_id"),
                       "kind": kind, "path": str(path)})
        log("[ROUTE] seq=%s %s %s → %s（%s）" % (seq, event.get("task_id"), kind,
                                                action, path.name))
    if fresh:
        set_last_seq(max(int(e.get("seq") or 0) for e in fresh))
    summary = {"ok": True, "routed": len(routed), "items": routed,
               "cursor": last_seq()}
    log("[SUMMARY] %s" % json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="邮箱事件路由守护（只读 + 通知）")
    parser.add_argument("--once", action="store_true", help="跑一轮就退出")
    parser.add_argument("--watch", action="store_true", help="循环执行")
    parser.add_argument("--interval", type=int, default=60, help="循环间隔秒")
    args = parser.parse_args()

    if args.watch:
        while True:
            try:
                one_round()
            except Exception as exc:
                log("[ERROR] %s: %s" % (type(exc).__name__, exc))
            time.sleep(max(args.interval, 15))
    one_round()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
