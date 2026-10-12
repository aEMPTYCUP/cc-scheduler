#!/usr/bin/env python3
"""调度器启动器(绕开 PowerShell 中文路径 bug)。

由 Windows 任务计划程序调用本脚本，由本脚本读取 prompt 文件，
用 subprocess 调 claude.cmd 执行调度轮次。

用法: python scheduler_launcher.py
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = r"C:\Users\32831\OneDrive\Desktop\期货分析"
PROMPT_FILE = os.path.join(ROOT, ".claude", "scheduler-prompt-current.txt")
LOG_FILE = os.path.join(ROOT, ".claude", "tools", "scheduler-run.log")
CLAUDE = r"C:\Users\32831\AppData\Roaming\npm\claude.cmd"


def main() -> None:
    os.chdir(ROOT)

    # 读取 prompt
    with open(PROMPT_FILE, "r", encoding="utf-8") as f:
        prompt = f.read().strip()

    # 追加时间戳到日志
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write(f"\n{'='*60}\n[{ts}] 调度轮次开始\n")

    # 调 claude --print --dangerously-skip-permissions
    result = subprocess.run(
        [CLAUDE, "--print", "--dangerously-skip-permissions", prompt],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )

    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write(f"stdout:\n{result.stdout}\n")
        if result.stderr:
            log.write(f"stderr:\n{result.stderr}\n")
        log.write(f"exit_code={result.returncode}\n")

    print(result.stdout, end="")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
