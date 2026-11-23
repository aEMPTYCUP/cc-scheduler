# -*- coding: utf-8 -*-
"""调度层：给某个豆包账号写「空闲信号」文件（退出闸门的唯一授权来源）。

什么时候用
----------
豆包账号的每小时定时任务只有在同时满足下面两条时才会「回到每小时读一次信箱」：
  1. 调度层显式给出空闲信号（本文档写的 signals/<owner>.json，idle=true）
  2. 该账号提交的任务全部审核通过（由 owner_worklist.py 现场核对）

所以：**派了新单就把信号收回（--idle false）**，队列真的排空、验收都过了才
发空闲信号（--idle true）。信号不是装饰，是闸门。

用法
----
    C:\\Users\\32831\\cc-scheduler\\.venv\\Scripts\\python.exe ^
        C:\\Users\\32831\\cc-scheduler\\phase1\\set_idle_signal.py ^
        --owner doubao-shenlanking --idle false --note "刚派了 T-188/T-251"

    ... --idle true --note "队列排空，验收全过"

安全阀：--idle true 时如果现场仍有待认领任务，脚本会把 idle 强制改回 false 并
在 reason 里写明，避免过期信号把账号误判成"没事了"。

只写 signals/<owner>.json 一个文件；不碰责任卡、不碰 queue.db、不碰邮箱。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from owner_worklist import (  # noqa: E402
    DEFAULT_BRIDGE_ROOT,
    DEFAULT_QUEUE_DB,
    DEFAULT_SIGNAL_DIRNAME,
    collect,
)


def _outbox_seq(bridge_root: Path) -> Optional[int]:
    path = bridge_root / "notifications" / "outbox.jsonl"
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Write the scheduler idle signal for one Doubao owner.")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--idle", default="false", choices=["true", "false"],
                        help="true = 授权该账号回到每小时读一次信箱")
    parser.add_argument("--note", default="", help="给账号看的一句话说明")
    parser.add_argument("--issued-by", default="dispatcher-local")
    parser.add_argument("--bridge-root", default=str(DEFAULT_BRIDGE_ROOT))
    parser.add_argument("--queue-db", default=str(DEFAULT_QUEUE_DB))
    parser.add_argument("--signal-dir", default=None)
    parser.add_argument("--parked", default="",
                        help="逗号分隔：明确登记为「挂在人身上」的任务 id（覆盖自动计算）")
    args = parser.parse_args(argv)

    bridge_root = Path(args.bridge_root)
    queue_db = Path(args.queue_db)
    signal_dir = Path(args.signal_dir) if args.signal_dir else bridge_root / DEFAULT_SIGNAL_DIRNAME

    view = collect(args.owner, bridge_root, queue_db, signal_dir)

    if args.parked.strip():
        parked = sorted({p.strip() for p in args.parked.split(",") if p.strip()})
    else:
        parked = sorted(set(view["parked"]) | {
            tid for tid, status in view["console_tasks"].items()
            if status == "NEEDS_HUMAN"
        })

    idle = args.idle == "true"
    forced = None
    if idle and view["pending"]:
        idle = False
        forced = "requested idle=true but pending=%s" % ",".join(view["pending"])

    payload = {
        "schema_version": 1,
        "owner": args.owner,
        "idle": idle,
        "issued_at_utc": datetime.now(timezone.utc).isoformat(),
        "issued_by": args.issued_by,
        "note": args.note,
        "parked_on_human": parked,
        "forced_off": forced,
        "facts": {
            "pending": view["pending"],
            "in_flight": view["in_flight"],
            "not_passed": view["not_passed"],
            "console_tasks": view["console_tasks"],
        },
        "anomalies": view["anomalies"],
        "outbox_seq": _outbox_seq(bridge_root),
        "errors": view["errors"],
    }

    signal_dir.mkdir(parents=True, exist_ok=True)
    path = signal_dir / ("%s.json" % args.owner)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)

    print(json.dumps({
        "ok": True,
        "owner": args.owner,
        "idle": idle,
        "forced_off": forced,
        "parked_on_human": parked,
        "pending": view["pending"],
        "in_flight": view["in_flight"],
        "not_passed": view["not_passed"],
        "anomalies": view["anomalies"],
        "signal_path": str(path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
