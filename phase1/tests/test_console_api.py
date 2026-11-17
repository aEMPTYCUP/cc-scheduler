#!/usr/bin/env python3
r"""HTTP-level tests for the orchestration console control endpoints.

Boots the real BoardHandler on an ephemeral loopback port in a daemon thread
(no supervisor) and drives it over HTTP. The conftest ``db_path`` fixture
redirects protocol.DB_PATH, and ui_server reads that path live per request, so
nothing touches a real database.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import orchestrator  # noqa: E402
import ui_server  # noqa: E402


@pytest.fixture
def http_server(db_path):
    orchestrator.ensure_orchestrator()
    server = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.BoardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _call(base: str, method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_data_payload_exposes_console_state(http_server) -> None:
    status, body = _call(http_server, "GET", "/api/data")
    assert status == 200
    assert [n["id"] for n in body["control"]["nodes"]] == list(orchestrator.NODES)
    executors = {e["executor_id"]: e for e in body["control"]["executors"]}
    assert executors["doubao"]["capabilities"]["acceptance"]["locked"] == 1


def test_locked_capability_returns_403(http_server) -> None:
    status, body = _call(http_server, "POST", "/api/capability",
                         {"executor_id": "doubao", "node": "acceptance", "enabled": True})
    assert status == 403
    assert "locked" in body["error"]


def test_capability_go_stop_flow(http_server) -> None:
    status, _ = _call(http_server, "POST", "/api/capability",
                      {"executor_id": "codex", "node": "acceptance", "enabled": True})
    assert status == 200
    status, body = _call(http_server, "POST", "/api/go",
                         {"executor_id": "doubao", "node": "implement"})
    assert status == 200 and body["result"]["running"] is True
    status, body = _call(http_server, "POST", "/api/stop",
                         {"executor_id": "doubao", "node": "implement"})
    assert status == 200 and body["result"]["running"] is False


def test_go_refused_when_capability_disabled_returns_409(http_server) -> None:
    _call(http_server, "POST", "/api/capability",
          {"executor_id": "cursor", "node": "implement", "enabled": False})
    status, body = _call(http_server, "POST", "/api/go",
                         {"executor_id": "cursor", "node": "implement"})
    assert status == 409
    assert "capability" in body["error"]


def test_missing_field_is_400_and_unknown_route_is_404(http_server) -> None:
    status, _ = _call(http_server, "POST", "/api/go", {"executor_id": "claude"})
    assert status == 400
    status, _ = _call(http_server, "POST", "/api/nope", {})
    assert status == 404


def test_reset_demo_reseeds_a_known_pool(http_server) -> None:
    status, body = _call(http_server, "POST", "/api/reset-demo", {})
    assert status == 200
    assert body["result"]["seeded"] == {"READY": 5, "REVIEW": 1}
    status, data = _call(http_server, "GET", "/api/data")
    counts: dict[str, int] = {}
    for task in data["tasks"]:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    assert counts.get("READY") == 5 and counts.get("REVIEW") == 1
    assert all(s["running"] == 0 for e in data["control"]["executors"]
               for s in e["sessions"].values())


def test_sim_config_updates_step_delay(http_server) -> None:
    status, body = _call(http_server, "POST", "/api/sim-config", {"step_delay": 2.5})
    assert status == 200 and body["result"]["sim_step_delay"] == 2.5
    status, data = _call(http_server, "GET", "/api/data")
    assert data["control"]["sim_step_delay"] == 2.5
    # Negative values are clamped to zero rather than rejected.
    status, body = _call(http_server, "POST", "/api/sim-config", {"step_delay": -3})
    assert status == 200 and body["result"]["sim_step_delay"] == 0
    orchestrator.set_sim_step_delay(0)  # do not slow later tests in this process
