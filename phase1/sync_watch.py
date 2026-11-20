#!/usr/bin/env python3
"""N-20: resident consumer that keeps the console pool projected from the
bridge outbox, so the board never drifts from the responsibility cards.

The UI only projects on GET /api/data; this watch process closes the gap by
calling bridge_sync.sync_once() on a fixed interval (safe, idempotent). Stop
with Ctrl-C or the orchestrator supervisor.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PHASE1_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PHASE1_DIR))

import bridge_sync  # noqa: E402

DEFAULT_BRIDGE_ROOT = bridge_sync.DEFAULT_BRIDGE_ROOT


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resident bridge-outbox projector")
    parser.add_argument("--bridge-root", default=str(DEFAULT_BRIDGE_ROOT),
                        help="bridge-live data root")
    parser.add_argument("--interval", type=int, default=30,
                        help="seconds between sync_once passes (default 30)")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    args = parser.parse_args(argv)

    while True:
        try:
            result = bridge_sync.sync_once(Path(args.bridge_root))
            print(f"sync_once ok={result['ok']} last_seq={result['last_seq']} "
                  f"applied={len(result['applied'])}", flush=True)
        except Exception as exc:  # noqa: BLE001 - keep the watcher alive
            print(f"sync_once error: {type(exc).__name__}: {exc}", flush=True)
        if args.once:
            return 0
        time.sleep(max(args.interval, 5))


if __name__ == "__main__":
    raise SystemExit(main())
