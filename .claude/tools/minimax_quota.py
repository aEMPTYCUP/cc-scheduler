#!/usr/bin/env python3
"""查询 MiniMax Coding Plan 5h 窗口额度(降级直连方案,替代 minimax-remaining-mcp)。

用法:
    python minimax_quota.py            # 人类可读输出
    python minimax_quota.py --json     # 机器可读 JSON
    python minimax_quota.py --code     # 退出码 0=可执行 1=应暂停

数据源: https://api.minimaxi.com/v1/token_plan/remains (Bearer key)
阈值: 5h 剩余 >= 20% 且 周剩余 >= 20% 才可调用 MiniMax/Codex。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

API_URL = "https://api.minimaxi.com/v1/token_plan/remains"
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env.minimax")
THRESHOLD_PCT = 20


def load_key() -> str:
    with open(KEY_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def query() -> dict:
    req = urllib.request.Request(
        API_URL, headers={"Authorization": f"Bearer {load_key()}"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse(data: dict) -> dict:
    """从 API 响应提取调度所需字段。"""
    base = data.get("base_resp", {})
    if base.get("status_code") != 0:
        return {"error": base.get("status_msg", "unknown error"), "should_pause": True}

    m = data["model_remains"][0]
    interval_pct = m.get("current_interval_remaining_percent", 0)
    weekly_pct = m.get("current_weekly_remaining_percent", 0)
    remains_ms = m.get("remains_time", 0)
    weekly_remains_ms = m.get("weekly_remains_time", 0)

    should_pause = interval_pct < THRESHOLD_PCT or weekly_pct < THRESHOLD_PCT

    return {
        "interval_remaining_pct": interval_pct,
        "weekly_remaining_pct": weekly_pct,
        "interval_remains_ms": remains_ms,
        "interval_remains_minutes": round(remains_ms / 60000, 1),
        "weekly_remains_minutes": round(weekly_remains_ms / 60000, 1),
        "threshold_pct": THRESHOLD_PCT,
        "should_pause": should_pause,
    }


def main() -> None:
    try:
        data = query()
        result = parse(data)
    except Exception as e:  # noqa: BLE001
        result = {"error": f"{type(e).__name__}: {e}", "should_pause": True}

    if "--json" in sys.argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif "--code" in sys.argv:
        # 退出码语义: 0=可执行, 1=应暂停/冷却, 2=查询出错
        if result.get("error"):
            print(json.dumps(result, ensure_ascii=False))
            sys.exit(2)
        sys.exit(1 if result.get("should_pause") else 0)
    else:
        if result.get("error"):
            print(f"[错误] {result['error']}")
            return
        print("MiniMax Coding Plan 额度状态")
        print(f"  5h 窗口剩余额度: {result['interval_remaining_pct']}% (阈值 {THRESHOLD_PCT}%)")
        print(f"  周剩余额度:       {result['weekly_remaining_pct']}%")
        print(f"  5h 窗口重置倒计时: {result['interval_remains_minutes']} 分钟")
        print(f"  周重置倒计时:      {result['weekly_remains_minutes']} 分钟")
        print(f"  判定: {'🟥 应暂停(冷却中)' if result['should_pause'] else '🟩 可执行任务'}")


if __name__ == "__main__":
    main()
