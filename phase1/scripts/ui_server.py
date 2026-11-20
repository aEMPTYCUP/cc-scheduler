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


# agent-bridge data root whose registry holds the concrete registered
# identities (e.g. the two Doubao accounts). Overridable via --bridge-root.
BRIDGE_ROOT = Path(r"C:/Users/32831/wt/bridge-live")


def _load_identities() -> dict:
    """Read registered bridge identities (read-only); degrade gracefully."""
    bridge_pkg = "C:/Users/32831/wt/agent-bridge-implementation/.claude"
    if bridge_pkg not in sys.path:
        sys.path.insert(0, bridge_pkg)
    try:
        from agent_bridge.agent_identity import list_agents, registry_path
        root = BRIDGE_ROOT
        if not registry_path(root).exists():
            return {"ok": True, "identities": [], "note": "no registry at this bridge root"}
        return {"ok": True, "identities": list_agents(root), "bridge_root": str(root)}
    except Exception as exc:  # never let identity display break the console
        return {"ok": False, "identities": [], "error": f"{type(exc).__name__}: {exc}"}


def _load_bridge_assignments() -> dict:
    """Map each console task to its bridge responsibility-card owner.

    The console tasks table only stores the execution *channel* (e.g. doubao);
    the concrete registered account (shenlanking / woaijiangjiang) lives on the
    agent-bridge card. This reads those cards directly (no BridgeStore rebuild,
    purely file reads) and joins registry labels, so a task card can show which
    Doubao account actually owns it.
    """
    bridge_pkg = "C:/Users/32831/wt/agent-bridge-implementation/.claude"
    if bridge_pkg not in sys.path:
        sys.path.insert(0, bridge_pkg)
    try:
        from agent_bridge.agent_identity import list_agents, registry_path
        root = BRIDGE_ROOT
        if not registry_path(root).exists():
            return {"ok": True, "assignments": {}}
        labels = {a["agent_id"]: a["label"] for a in list_agents(root)}
        assignments: dict = {}
        tasks_dir = Path(root) / "tasks"
        if tasks_dir.is_dir():
            for card_path in sorted(tasks_dir.glob("*.json")):
                card = json.loads(card_path.read_text(encoding="utf-8"))
                execution = card.get("execution", {})
                owner = execution.get("owner")
                if not owner:
                    continue
                assignments[card["task_id"]] = {
                    "owner": owner,
                    "owner_label": labels.get(owner, owner),
                    "card_status": execution.get("status"),
                    "attempt": execution.get("attempt"),
                    "run_id": execution.get("run_id"),
                }
        return {"ok": True, "assignments": assignments}
    except Exception as exc:  # never break the board if the mailbox is unreadable
        return {"ok": False, "assignments": {}, "error": f"{type(exc).__name__}: {exc}"}


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
            try:  # event-driven projection of bridge card transitions
                import bridge_sync
                bridge_sync.sync_once(BRIDGE_ROOT)
            except Exception as exc:  # never let projection break the board
                payload["bridge_sync_error"] = f"{type(exc).__name__}: {exc}"
            payload["bridge"] = _load_bridge_assignments()
            self._json(200, payload)
            return
        if path == "/api/identities":
            self._json(200, _load_identities())
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
            elif path == "/api/create":
                # N-06: task creation form endpoint — real protocol path, never
                # a demo JSON edit. Creates a BACKLOG task; `ready: true` moves
                # it to READY for the scheduler to pick up.
                task_id = (body.get("task_id") or "").strip()
                title = (body.get("title") or "").strip()
                if not task_id or not title:
                    self._json(400, {"ok": False, "error": "缺少 task_id 或 title"})
                    return
                db = _db_path()
                if not db.exists():
                    self._json(409, {"ok": False, "error": "没有本地任务库，演示数据不能创建任务"})
                    return
                allowed = body.get("allowed_paths") or []
                if isinstance(allowed, str):
                    allowed = [p.strip() for p in allowed.split(",") if p.strip()]
                if not isinstance(allowed, list):
                    self._json(400, {"ok": False, "error": "allowed_paths 必须是数组或逗号分隔字符串"})
                    return
                protocol.create_task(
                    task_id, title, body.get("description") or "",
                    body.get("role_id") or "r1", body.get("acceptance_criteria") or "",
                    priority=body.get("priority") or "medium",
                    allowed_paths=allowed,
                    behavioral_check=body.get("behavioral_check") or "",
                    pass_condition=body.get("pass_condition") or "",
                )
                if body.get("ready"):
                    protocol.ready_task(task_id)
                task = protocol.get_task(task_id)
                result = {"task_id": task_id, "status": task["status"] if task else "?"}
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
        except orchestrator.RealPoolSimulationGuard as exc:
            self._json(403, {"ok": False, "error": str(exc), "code": "SIM_GO_BLOCKED"})
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
    parser.add_argument("--adapter", choices=("simulated", "doubao", "claude"), default="simulated",
                        help="simulated (default, blocked on production), doubao mailbox pull, "
                             "or headless claude subprocess (unattended implement)")
    parser.add_argument("--bridge-root", default="C:/Users/32831/wt/bridge-live",
                        help="agent-bridge data root for the real Doubao adapter")
    parser.add_argument("--doubao-owner", default="doubao-shenlanking",
                        help="registered bridge identity the Doubao adapter dispatches to")
    parser.add_argument("--worktree-root", default="C:/Users/32831/wt",
                        help="parent dir under which per-task git worktrees are created")
    parser.add_argument("--repo-path",
                        default="C:/Users/32831/OneDrive/Desktop/期货分析",
                        help="target repository for headless claude worktrees")
    parser.add_argument("--permission-mode", default="acceptEdits",
                        choices=("acceptEdits", "bypassPermissions", "default", "plan"),
                        help="headless claude permission mode (bypassPermissions enables full autonomy)")
    args = parser.parse_args(argv)

    global BRIDGE_ROOT
    BRIDGE_ROOT = Path(args.bridge_root)

    orchestrator.set_sim_step_delay(args.sim_step_delay)
    orchestrator.ensure_orchestrator()

    def _adapter_factory():
        if args.adapter == "doubao":
            # Lazy import keeps the simulated default free of the agent_bridge dependency.
            bridge_pkg = "C:/Users/32831/wt/agent-bridge-implementation/.claude"
            if bridge_pkg not in sys.path:
                sys.path.insert(0, bridge_pkg)
            from real_adapters import DoubaoAdapter
            return DoubaoAdapter(
                bridge_root=args.bridge_root, owner=args.doubao_owner,
                worktree_root=args.worktree_root,
            )
        if args.adapter == "claude":
            from real_adapters import ClaudeSubprocessAdapter
            return ClaudeSubprocessAdapter(
                repo_path=args.repo_path, permission_mode=args.permission_mode,
            )
        return orchestrator.SimulatedAdapter()
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
        # Register whether the active adapter is simulated so the production
        # pool guard refuses fake GO runs but admits a real adapter.
        is_sim = args.adapter == "simulated"
        orchestrator.configure_adapter(is_sim)
        supervisor = orchestrator.WorkerSupervisor(adapter_factory=_adapter_factory)
        supervisor.start()

    server = ThreadingHTTPServer((args.host, args.port), BoardHandler)
    print(f"Orchestration console at http://{args.host}:{args.port}/  "
          f"({source}, {len(payload.get('tasks') or [])} tasks, adapter={args.adapter})", flush=True)
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
