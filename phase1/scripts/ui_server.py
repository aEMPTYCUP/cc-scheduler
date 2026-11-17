#!/usr/bin/env python3
"""Serve the Phase 1 orchestration console locally and accept control actions.

Read side: GET /api/data returns the protocol task/audit snapshot plus the
orchestrator console state (per-node capability switches and GO/STOP sessions).

Write side (the control plane):
  POST /api/capability  {executor_id, node, enabled}  toggle a node switch
  POST /api/go          {executor_id, node}           start a worker loop
  POST /api/stop        {executor_id, node}           stop a worker loop
  POST /api/integrate   {task_id}                     human ACCEPTED -> DONE
  POST /api/reset-demo                                reseed a clean demo pool
  POST /api/submit      {task_id, actor}              legacy mark-submitted

A single WorkerSupervisor daemon thread ticks every running session. All state
changes go through protocol/orchestrator, which only touch the local SQLite DB
passed via PHASE1_DB; the formal tasks/queue.json is never involved.
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
import orchestrator  # noqa: E402
import protocol  # noqa: E402


def _db_path() -> Path:
    """Live DB path so tests can redirect protocol.DB_PATH after import."""
    return protocol.DB_PATH


class BoardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise ValueError("请求不是有效 JSON")
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return body

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/data":
            db = _db_path()
            if db.exists():
                payload = export_for_ui.load_from_db(db)
            else:
                payload = export_for_ui.load_demo()
            payload["live"] = True
            payload["read_only"] = False
            payload["control"] = orchestrator.console_state()
            self._json(200, payload)
            return
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_body()
            if path == "/api/capability":
                result = orchestrator.set_capability(
                    body["executor_id"], body["node"], bool(body["enabled"]))
            elif path == "/api/go":
                result = orchestrator.start_worker(body["executor_id"], body["node"])
            elif path == "/api/stop":
                result = orchestrator.stop_worker(body["executor_id"], body["node"])
            elif path == "/api/integrate":
                task = orchestrator.integrate_task(body["task_id"], body.get("actor", "human"))
                result = {"task_id": task["task_id"], "status": task["status"]}
            elif path == "/api/reset-demo":
                result = {"seeded": orchestrator.seed_demo_pool()}
            elif path == "/api/sim-config":
                delay = float(body.get("step_delay", 0))
                result = {"sim_step_delay": orchestrator.set_sim_step_delay(delay)}
            elif path == "/api/submit":
                task_id = (body.get("task_id") or "").strip()
                actor = (body.get("actor") or "board-operator").strip() or "board-operator"
                if not task_id:
                    self._json(400, {"ok": False, "error": "缺少 task_id"})
                    return
                db = _db_path()
                if not db.exists():
                    self._json(409, {"ok": False, "error": "没有本地任务库，演示数据不能改状态"})
                    return
                task = submitter.mark_submitted(task_id, actor)
                result = {"task_id": task["task_id"], "status": task["status"]}
            else:
                self._json(404, {"ok": False, "error": "unknown endpoint"})
                return
            self._json(200, {"ok": True, "result": result, "control": orchestrator.console_state()})
        except orchestrator.CapabilityLocked as exc:
            self._json(403, {"ok": False, "error": str(exc)})
        except KeyError as exc:
            self._json(400, {"ok": False, "error": f"缺少字段 {exc}"})
        except (ValueError, orchestrator.WorkerError) as exc:
            self._json(409, {"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - surface, never crash the server
            self._json(500, {"ok": False, "error": f"操作失败：{exc}"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the Phase 1 orchestration console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-supervisor", action="store_true",
                        help="Do not start the background worker supervisor (tests)")
    parser.add_argument("--sim-step-delay", type=float, default=1.5,
                        help="Seconds a simulated worker lingers per node (0 = instant; tests stay instant)")
    args = parser.parse_args(argv)

    orchestrator.set_sim_step_delay(args.sim_step_delay)
    orchestrator.ensure_orchestrator()
    db = _db_path()
    if db.exists():
        payload = export_for_ui.load_from_db(db)
        export_for_ui.write_payload(payload, export_for_ui.DEFAULT_OUT)
        source = f"sqlite:{db}"
    else:
        payload = export_for_ui.load_demo()
        export_for_ui.write_payload(payload, export_for_ui.DEFAULT_OUT)
        source = "demo"

    supervisor = None
    if not args.no_supervisor:
        supervisor = orchestrator.WorkerSupervisor()
        supervisor.start()

    server = ThreadingHTTPServer((args.host, args.port), BoardHandler)
    print(f"Orchestration console at http://{args.host}:{args.port}/  "
          f"({source}, {len(payload.get('tasks') or [])} tasks)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping")
    finally:
        if supervisor:
            supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
