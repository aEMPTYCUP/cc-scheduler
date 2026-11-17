#!/usr/bin/env python3
"""Serve the Phase 1 board locally and accept a submit action.

Usage (from phase1/):

    python3 scripts/ui_server.py

Then open http://127.0.0.1:8765/ . The "标记为已提交" button posts here,
which calls mark_submitted.py's logic against /tmp/phase1_protocol.db
and refreshes the JSON snapshot. Demo data is not modified.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PHASE1_DIR = Path(__file__).resolve().parents[1]
UI_DIR = PHASE1_DIR / "ui"
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(PHASE1_DIR))

import export_for_ui  # noqa: E402
import mark_submitted as submitter  # noqa: E402


class BoardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/data":
            db = export_for_ui.DEFAULT_DB
            if db.exists():
                payload = export_for_ui.load_from_db(db)
            else:
                payload = export_for_ui.load_demo()
            payload["live"] = True
            self._json(200, payload)
            return
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/submit":
            self._json(404, {"ok": False, "error": "unknown endpoint"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "请求不是有效 JSON"})
            return
        task_id = (body.get("task_id") or "").strip()
        actor = (body.get("actor") or "board-operator").strip() or "board-operator"
        if not task_id:
            self._json(400, {"ok": False, "error": "缺少 task_id"})
            return
        db = export_for_ui.DEFAULT_DB
        if not db.exists():
            self._json(409, {"ok": False, "error": "没有本地任务库，演示数据不能改状态"})
            return
        try:
            task = submitter.mark_submitted(task_id, actor)
            payload = export_for_ui.load_from_db(db)
            export_for_ui.write_payload(payload, export_for_ui.DEFAULT_OUT)
            payload["live"] = True
            self._json(200, {"ok": True, "task": {"task_id": task["task_id"], "status": task["status"]}, "data": payload})
        except ValueError as exc:
            self._json(409, {"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - unexpected
            self._json(500, {"ok": False, "error": f"提交失败：{exc}"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the Phase 1 board")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if export_for_ui.DEFAULT_DB.exists():
        payload = export_for_ui.load_from_db(export_for_ui.DEFAULT_DB)
        export_for_ui.write_payload(payload, export_for_ui.DEFAULT_OUT)
        source = f"sqlite:{export_for_ui.DEFAULT_DB}"
    else:
        payload = export_for_ui.load_demo()
        export_for_ui.write_payload(payload, export_for_ui.DEFAULT_OUT)
        source = "demo"
    server = ThreadingHTTPServer((args.host, args.port), BoardHandler)
    print(f"Board at http://{args.host}:{args.port}/  ({source}, {len(payload.get('tasks') or [])} tasks)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
