#!/usr/bin/env python3
r"""Pure unit tests for the MiniMax reviewer's behavioral-check parser.

These cover the three review defects found on the real Doubao tasks:

* T-252: expected-result prose glued after a command ("vue-tsc --noEmit 退出码 0。")
  was passed as argv, so vue-tsc compiled the prose as files (TS6231, exit 2).
* T-221: an ASCII arrow ("verify.py -> pass=4 ...") tripped the ">" forbidden
  token guard, so a real read-only verify script was skipped wholesale.
* T-201/T-206: pure-prose acceptance criteria carry no executable command; the
  missing exit code is a spec property and must not by itself force REWORK.

No subprocess and no network: the shell call is monkeypatched and the parser
helpers are exercised directly.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import minimax_reviewer as mr


def test_strip_cjk_prose_suffix():
    assert mr._strip_inline_prose("npx vue-tsc --noEmit 退出码 0。") == "npx vue-tsc --noEmit"
    # The cd prefix is ASCII and must survive untouched.
    assert mr._strip_inline_prose("cd frontend && npx vue-tsc --noEmit 退出码 0。") \
        == "cd frontend && npx vue-tsc --noEmit"
    assert mr._strip_inline_prose("node scripts/smoke.mjs 退出码 0 且断言命中") \
        == "node scripts/smoke.mjs"


def test_strip_arrow_prose_suffix():
    assert mr._strip_inline_prose(
        "python .agent-audits/evidence/EV-T-221-verify.py -> pass=4 blocked=2 fail=0 (rc=0)"
    ) == "python .agent-audits/evidence/EV-T-221-verify.py"


def test_strip_leaves_clean_commands_and_prose_only():
    assert mr._strip_inline_prose("node scripts/smoke.mjs") == "node scripts/smoke.mjs"
    assert mr._strip_inline_prose("pytest -q tests/test_x.py") == "pytest -q tests/test_x.py"
    # A leading English prose word survives the cut (first CJK is later), but the
    # allow-list rejects it downstream, so it is never executed.
    assert mr._strip_inline_prose("canvas 可键盘聚焦；aria 完整") == "canvas"
    assert mr._has_runnable_check({"behavioral_check": "canvas 可键盘聚焦；aria 完整"}) is False


def test_has_runnable_check_classifies_specs():
    t252 = {"behavioral_check":
            "cd backend && node scripts/smoke.mjs 退出码 0；grep 无警告；"
            "cd frontend && npx vue-tsc --noEmit 退出码 0。"}
    t221 = {"behavioral_check":
            "python .agent-audits/evidence/EV-T-221-verify.py -> pass=4 fail=0 (rc=0)"}
    t206 = {"behavioral_check": "canvas 可键盘聚焦；aria 完整；sr-only 表 66 行；aria-live 播报。"}
    grep_only = {"behavioral_check": "grep smoke 输出无 import-is-undefined 警告"}
    assert mr._has_runnable_check(t252) is True
    assert mr._has_runnable_check(t221) is True
    assert mr._has_runnable_check(t206) is False
    assert mr._has_runnable_check(grep_only) is False
    assert mr._has_runnable_check({"behavioral_check": ""}) is False


def _fake_run_factory(calls):
    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
    return _fake_run


def test_run_passes_cleaned_command_without_prose(monkeypatch):
    calls = []
    monkeypatch.setattr(mr.subprocess, "run", _fake_run_factory(calls))
    task = {"behavioral_check":
            "cd backend && node scripts/smoke.mjs 退出码 0 且断言命中；"
            "cd frontend && npx vue-tsc --noEmit 退出码 0。"}
    mr._run_behavioral_checks(task, mr.Path("/wt/T-252"))
    # Both allow-listed commands ran; neither carried CJK prose into argv.
    assert calls == [
        "cd backend && node scripts/smoke.mjs",
        "cd frontend && npx vue-tsc --noEmit",
    ]


def test_run_arrow_does_not_trip_forbidden_guard(monkeypatch):
    calls = []
    monkeypatch.setattr(mr.subprocess, "run", _fake_run_factory(calls))
    task = {"behavioral_check":
            "python .agent-audits/evidence/EV-T-221-verify.py -> pass=4 fail=0 (rc=0)"}
    out = mr._run_behavioral_checks(task, mr.Path("/wt/T-221"))
    # The verify script actually ran (not "skipped: forbidden token") and the
    # arrow prose was removed before the (venv-rewritten) interpreter call.
    assert len(calls) == 1
    assert calls[0].endswith(".agent-audits/evidence/EV-T-221-verify.py")
    assert "->" not in calls[0]
    assert "forbidden" not in out


def test_dangerous_commands_still_blocked(monkeypatch):
    calls = []
    monkeypatch.setattr(mr.subprocess, "run", _fake_run_factory(calls))
    # A real shell redirect keeps ">" (only the "->" arrow is prose-stripped).
    task = {"behavioral_check": "python x.py > out.txt；python y.py && rm -rf z"}
    out = mr._run_behavioral_checks(task, mr.Path("/wt/T-x"))
    assert calls == []  # nothing dangerous ever reaches the shell
    assert out.count("forbidden token") == 2


def test_pytest_runs_get_hermetic_env(monkeypatch):
    captured = []

    def _fake_run(cmd, **kwargs):
        captured.append((cmd, kwargs.get("env")))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(mr.subprocess, "run", _fake_run)
    task = {"behavioral_check": "pytest -q tests/test_x.py；node scripts/smoke.mjs"}
    mr._run_behavioral_checks(task, mr.Path("/wt/T-1"))
    assert len(captured) == 2
    pytest_env = captured[0][1]
    addopts = pytest_env["PYTEST_ADDOPTS"]
    assert "-o addopts=" in addopts and "--basetemp" in addopts
    # The same hermetic env is handed to node too (the var is simply ignored).
    assert captured[1][1] is pytest_env


def test_prompt_branches_on_runnable_check():
    base_task = {"task_id": "T-1", "title": "t", "pass_condition": "pc",
                 "allowed_paths_json": "[]", "behavioral_check": ""}
    ev_with = {"changed": [], "diff_stat": "", "diff": "", "file_contents": "",
               "checks": "$ pytest\n[exit=0]", "has_runnable_check": True}
    ev_without = {"changed": ["a.vue"], "diff_stat": "1 file", "diff": "d",
                  "file_contents": "full", "checks": "", "has_runnable_check": False}
    prompt_with = mr._build_review_prompt(base_task, ev_with)[1]["content"]
    prompt_without = mr._build_review_prompt(base_task, ev_without)[1]["content"]
    assert "exit code 是否为 0" in prompt_with
    assert "不得仅凭“无 exit code”判 REWORK" in prompt_without
    assert "exit code 是否为 0" not in prompt_without


def test_prompt_clarifies_committed_only_boundary():
    task = {"task_id": "T-1", "title": "t", "pass_condition": "pc",
            "allowed_paths_json": '["a.vue"]', "behavioral_check": ""}
    ev = {"changed": ["a.vue"], "diff_stat": "", "diff": "", "file_contents": "",
          "checks": "", "has_runnable_check": False}
    body = mr._build_review_prompt(task, ev)[1]["content"]
    # Untracked controller artifacts must not be mistaken for scope violations.
    assert "DISPATCH-" in body and "未跟踪" in body and "已提交清单" in body


def test_hermetic_env_uses_stable_basetemp():
    env = mr._hermetic_check_env()
    addopts = env["PYTEST_ADDOPTS"]
    assert "-o addopts=" in addopts and "--basetemp" in addopts
    # Stable name (no random mkdtemp suffix) so repeated ticks don't litter.
    assert "ccs-review-basetemp" in addopts
    import tempfile
    from pathlib import Path
    assert Path(addopts.split("--basetemp", 1)[1].strip()) == (
        Path(tempfile.gettempdir()) / "ccs-review-basetemp")


def test_gather_evidence_keeps_head_slice_of_oversized_file(monkeypatch, tmp_path):
    wt = tmp_path / "wt"
    rel = "frontend/src/components/Big.vue"
    big = wt / rel
    big.parent.mkdir(parents=True)
    head = "HEADMARKER" * 50
    big.write_text(head + "Z" * (mr.MAX_FILE_CHARS + 4096), encoding="utf-8")

    def fake_git(worktree, *args):
        if args[0] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")
        if args[0] == "diff" and "--name-only" in args:
            return SimpleNamespace(returncode=0, stdout=rel + "\n", stderr="")
        if args[0] == "diff" and "--stat" in args:
            return SimpleNamespace(returncode=0, stdout="1 file changed\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="DIFFBODY", stderr="")

    monkeypatch.setattr(mr, "_worktree_for", lambda tid: wt)
    monkeypatch.setattr(mr, "_git", fake_git)
    monkeypatch.setattr(mr, "_run_behavioral_checks", lambda task, w: "CHECKS")

    ev = mr._gather_evidence({"task_id": "T-9", "behavioral_check": ""})
    assert ev["changed"] == [rel]
    assert ev["head"] == "deadbeef" and ev["checks"] == "CHECKS"
    # The oversized core file is still represented (not silently skipped)...
    assert "Big.vue" in ev["file_contents"]
    assert head[:60] in ev["file_contents"]
    # ...with an explicit truncation marker, and bounded to the per-file cap.
    assert "仅提供前" in ev["file_contents"]
    assert "Z" * (mr.MAX_FILE_CHARS + 100) not in ev["file_contents"]


# --- three-way verdict (ACCEPTED / NEEDS_HUMAN / REWORK) ------------------- #

@pytest.mark.parametrize(("token", "expected"), [
    ("ACCEPTED", "ACCEPTED"), ("accept", "ACCEPTED"),
    ("NEEDS_HUMAN", "NEEDS_HUMAN"), ("needs human", "NEEDS_HUMAN"),
    ("REWORK", "REWORK"), ("REJECT", "REWORK"), ("", "REWORK"),
])
def test_normalize_verdict_three_way(token, expected):
    assert mr._normalize_verdict(token) == expected


def test_parse_verdict_handles_needs_human_json_and_fallback():
    payload = "<think>reasoning</think>```json\n" + json.dumps(
        {"verdict": "needs_human", "confidence": 0.8, "blocking_issues": ["人工签字"]}) + "\n```"
    assert mr._parse_verdict(payload)["verdict"] == "NEEDS_HUMAN"
    # Malformed JSON -> keyword fallback still recovers the three-way decision.
    bad = '{"verdict": "NEEDS_HUMAN", "confidence": 0.7, '  # truncated, no closing brace
    assert mr._parse_verdict(bad)["verdict"] == "NEEDS_HUMAN"


def test_prompt_declares_three_way_rubric():
    task = {"task_id": "T-1", "title": "t", "pass_condition": "pc",
            "allowed_paths_json": "[]", "behavioral_check": ""}
    ev = {"changed": [], "diff_stat": "", "diff": "", "file_contents": "",
          "checks": "", "has_runnable_check": False}
    messages = mr._build_review_prompt(task, ev)
    # The verdict enum lives in the system instruction; the rubric in the body.
    assert "ACCEPTED|NEEDS_HUMAN|REWORK" in messages[0]["content"]
    body = messages[1]["content"]
    assert "三态判定口径" in body and "凭据轮换" in body


class _StubClient:
    def __init__(self, token):
        self.token = token

    def chat(self, messages):
        return json.dumps({"verdict": self.token, "confidence": 0.9,
                           "reasons": [], "blocking_issues": ["note"]})


@pytest.mark.parametrize(("token", "console_status", "bridge_stage"), [
    ("ACCEPTED", "ACCEPTED", "needs_human"),
    ("NEEDS_HUMAN", "NEEDS_HUMAN", "needs_human"),
    ("REWORK", "REWORK", "retry_requested"),
])
def test_review_once_routes_three_verdicts(db_path, monkeypatch, token, console_status, bridge_stage):
    import protocol
    role_id, impl = "A3-backend-engineer", "doubao-owner"
    protocol.init_db()
    protocol.register_role(role_id)
    tid = f"T-rv-{token.lower()}"
    protocol.create_task(tid, title="routing", description="d", role_id=role_id,
                         acceptance_criteria="c")
    protocol.ready_task(tid)
    protocol.atomic_claim(tid, agent_id=impl, executor_id="doubao")
    protocol.transition(tid, "IN_PROGRESS", impl, "work")
    protocol.transition(tid, "SUBMITTED", impl, "done")
    protocol.transition(tid, "REVIEW", impl, "submit")

    monkeypatch.setattr(mr, "_gather_evidence",
                        lambda task: {"head": None, "changed": [], "diff_stat": "", "diff": "",
                                      "file_contents": "", "checks": "", "has_runnable_check": False})
    bridge_calls = []
    monkeypatch.setattr(mr, "_update_bridge_card",
                        lambda t, stage, reason: bridge_calls.append((t, stage)))

    outcomes = mr.review_once(_StubClient(token))
    assert outcomes == [{"task_id": tid, "verdict": token, "bridge_sync": "ok"}]
    assert protocol.get_task(tid)["status"] == console_status
    assert bridge_calls == [(tid, bridge_stage)]


# --- bridge card mirroring: headless (card-less) tasks must not error ------ #

def _install_fake_agent_bridge(monkeypatch, load_side_effect=None, card=None):
    import sys
    import types

    emitted = []
    updated = []

    class _FakeStore:
        def __init__(self, root):
            self.root = root

        def load_task(self, tid):
            if load_side_effect is not None:
                raise load_side_effect
            return card

        def update_task(self, tid, mutate):
            updated.append((tid, mutate(card)))

    fake_pkg = types.ModuleType("agent_bridge")
    fake_notif = types.ModuleType("agent_bridge.notifications")
    fake_resp = types.ModuleType("agent_bridge.responsibility")
    fake_store = types.ModuleType("agent_bridge.store")

    fake_notif.emit = lambda *a, **k: emitted.append(k)
    fake_resp._changed = lambda c, stage, actor, run, now: {
        "execution": {**c.get("execution", {}), "status": stage}}
    fake_resp.utc_now = lambda: "now"
    fake_store.BridgeStore = _FakeStore

    monkeypatch.setitem(sys.modules, "agent_bridge", fake_pkg)
    monkeypatch.setitem(sys.modules, "agent_bridge.notifications", fake_notif)
    monkeypatch.setitem(sys.modules, "agent_bridge.responsibility", fake_resp)
    monkeypatch.setitem(sys.modules, "agent_bridge.store", fake_store)
    return emitted, updated


def test_update_bridge_card_missing_card_is_silent_noop(monkeypatch):
    # A headless/CLI task has no responsibility card: FileNotFoundError must be
    # a quiet no-op (None), not the noisy error string seen on T-207.
    emitted, updated = _install_fake_agent_bridge(
        monkeypatch, load_side_effect=FileNotFoundError("T-207.json"))
    assert mr._update_bridge_card("T-207", "needs_human", "r") is None
    assert emitted == [] and updated == []


def test_update_bridge_card_syncs_when_card_exists(monkeypatch):
    card = {"execution": {"status": "in_progress", "run_id": "run-1", "owner": "doubao-x"}}
    emitted, updated = _install_fake_agent_bridge(monkeypatch, card=card)
    assert mr._update_bridge_card("T-9", "retry_requested", "fix it") is None
    assert len(updated) == 1 and emitted and emitted[0]["card_status"] == "retry_requested"


def test_update_bridge_card_other_errors_still_reported(monkeypatch):
    emitted, _ = _install_fake_agent_bridge(
        monkeypatch, load_side_effect=ValueError("boom"))
    err = mr._update_bridge_card("T-9", "needs_human", "r")
    assert err is not None and "ValueError" in err and emitted == []


@pytest.mark.parametrize(("current", "target", "expected"), [
    ("dispatched", "retry_requested", ["in_progress", "retry_requested"]),
    ("claimed", "retry_requested", ["in_progress", "retry_requested"]),
    ("needs_human", "retry_requested", ["in_progress", "retry_requested"]),
    ("in_progress", "retry_requested", ["retry_requested"]),
    ("awaiting_review", "retry_requested", ["retry_requested"]),
    ("dispatched", "needs_human", ["needs_human"]),
    ("claimed", "needs_human", ["needs_human"]),
    ("in_progress", "needs_human", ["needs_human"]),
])
def test_bridge_stage_sequence_uses_legal_hops(current, target, expected):
    assert mr._bridge_stage_sequence(current, target) == expected


def test_update_bridge_card_routes_early_stage_through_in_progress(monkeypatch):
    card = {"execution": {"status": "dispatched", "run_id": "r", "owner": "o"}}
    emitted, updated = _install_fake_agent_bridge(monkeypatch, card=card)
    assert mr._update_bridge_card("T-9", "retry_requested", "fix") is None
    # Two legal hops instead of an illegal dispatched -> retry_requested jump.
    assert len(updated) == 2
    assert emitted and emitted[0]["card_status"] == "retry_requested"


def test_prompt_states_pytest_discipline_and_baseline_rule():
    task = {"task_id": "T-1", "title": "t", "pass_condition": "pc",
            "allowed_paths_json": "[]", "behavioral_check": ""}
    ev = {"changed": [], "diff_stat": "", "diff": "", "file_contents": "",
          "checks": "", "has_runnable_check": False}
    body = mr._build_review_prompt(task, ev)[1]["content"]
    assert "-o addopts=" in body and "--basetemp" in body
    assert "环境假象" in body and "基线对比" in body


# --- evidence budget + auto-run of whitelisted tests ---------------------- #

def test_bare_pytest_is_not_treated_as_a_task_check():
    # A whole-suite run reflects unrelated files, repo addopts and machine
    # load; prose like "pytest 覆盖 quadrant 标准门必须全绿" must not execute.
    assert mr._is_bare_pytest("pytest") is True
    assert mr._is_bare_pytest("python -m pytest") is True
    assert mr._is_bare_pytest("pytest -q") is True
    assert mr._is_bare_pytest("pytest -q tests/test_x.py") is False
    assert mr._is_bare_pytest("python -m pytest tests/test_x.py") is False
    assert mr._is_bare_pytest("node scripts/smoke.mjs") is False


def test_auto_test_files_picks_existing_whitelisted_tests(monkeypatch, tmp_path):
    wt = tmp_path / "wt"
    (wt / "backend" / "tests").mkdir(parents=True)
    (wt / "backend" / "tests" / "test_pseudo_log.py").write_text("def test_x(): pass", encoding="utf-8")
    (wt / "backend" / "app").mkdir(parents=True)
    (wt / "backend" / "app" / "service.py").write_text("x = 1", encoding="utf-8")
    task = {"task_id": "T-1", "allowed_paths_json": json.dumps([
        "backend/tests/test_pseudo_log.py",
        "backend/app/service.py",
        "backend/tests/test_missing.py"])}
    assert mr._auto_test_files(task, wt) == ["backend/tests/test_pseudo_log.py"]


def test_run_whitelisted_tests_reports_exit_code(monkeypatch, tmp_path):
    captured = []

    def _fake_run(cmd, **kwargs):
        captured.append((cmd, kwargs.get("env")))
        return SimpleNamespace(returncode=0, stdout="2 passed\n", stderr="")

    monkeypatch.setattr(mr.subprocess, "run", _fake_run)
    wt = tmp_path / "wt"
    (wt / "backend" / "tests").mkdir(parents=True)
    (wt / "backend" / "tests" / "test_a.py").write_text("def test_x(): pass", encoding="utf-8")
    task = {"task_id": "T-1", "allowed_paths_json": json.dumps(["backend/tests/test_a.py"])}
    out = mr._run_whitelisted_tests(task, wt)
    assert "[exit=0]" in out
    assert captured and "-o addopts=" in captured[0][0]
    assert "PYTEST_ADDOPTS" in captured[0][1]


def test_gather_evidence_prioritises_small_files_and_names_skipped(monkeypatch, tmp_path):
    wt = tmp_path / "wt"
    (wt / "big.vue").mkdir(parents=True)
    (wt / "big.vue" / "x.txt").write_text("B" * 100, encoding="utf-8")
    (wt / "small.ts").mkdir(parents=True)
    (wt / "small.ts" / "y.txt").write_text("S" * 10, encoding="utf-8")
    names = ["big.vue/x.txt", "small.ts/y.txt"]

    def fake_git(worktree, *args):
        if args[0] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="sha\n", stderr="")
        if args[0] == "diff" and "--name-only" in args:
            return SimpleNamespace(returncode=0, stdout="\n".join(names) + "\n", stderr="")
        if args[0] == "diff" and "--stat" in args:
            return SimpleNamespace(returncode=0, stdout="2 files\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="DIFF", stderr="")

    monkeypatch.setattr(mr, "_worktree_for", lambda tid: wt)
    monkeypatch.setattr(mr, "_git", fake_git)
    monkeypatch.setattr(mr, "_run_behavioral_checks", lambda task, w: "")
    monkeypatch.setattr(mr, "_run_whitelisted_tests", lambda task, w: "")

    ev = mr._gather_evidence({"task_id": "T-9", "behavioral_check": "",
                               "allowed_paths_json": "[]"})
    # The small file must appear even though the big one is listed first.
    body = ev["file_contents"]
    assert "small.ts/y.txt" in body
    assert body.index("small.ts/y.txt") < body.index("big.vue/x.txt")


def test_gather_evidence_names_files_skipped_by_budget(monkeypatch, tmp_path):
    wt = tmp_path / "wt"
    rel = "huge.txt"
    wt.mkdir(parents=True)
    (wt / rel).write_text("H" * (mr.MAX_FILE_CHARS + 5000), encoding="utf-8")

    def fake_git(worktree, *args):
        if args[0] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="sha\n", stderr="")
        if args[0] == "diff" and "--name-only" in args:
            return SimpleNamespace(returncode=0, stdout=rel + "\n", stderr="")
        if args[0] == "diff" and "--stat" in args:
            return SimpleNamespace(returncode=0, stdout="1 file\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="DIFF", stderr="")

    monkeypatch.setattr(mr, "_worktree_for", lambda tid: wt)
    monkeypatch.setattr(mr, "_git", fake_git)
    monkeypatch.setattr(mr, "_run_behavioral_checks", lambda task, w: "")
    monkeypatch.setattr(mr, "_run_whitelisted_tests", lambda task, w: "")

    monkeypatch.setattr(mr, "MAX_TOTAL_FILE_CHARS", 100)
    ev = mr._gather_evidence({"task_id": "T-9", "behavioral_check": "",
                               "allowed_paths_json": "[]"})
    # The file does not fit the total budget at all, so it is named explicitly
    # in the skip list instead of silently vanishing.
    assert "因证据预算未提供完整内容" in ev["file_contents"]
    assert rel in ev["file_contents"]


# --- HTTP 429 resilience: Retry-After, backoff, typed error, circuit breaker - #

def _http_error(code: int, retry_after: "str | None" = None):
    import email
    import urllib.error
    hdrs = email.message_from_string(
        f"Retry-After: {retry_after}\r\n" if retry_after else "")
    return urllib.error.HTTPError(
        url="http://proxy/chat/completions", code=code,
        msg="Too Many Requests" if code == 429 else "err", hdrs=hdrs, fp=None)


def test_retry_after_header_parsed_or_absent():
    assert mr._retry_after_seconds(_http_error(429, "12")) == 12.0
    assert mr._retry_after_seconds(_http_error(429)) is None
    assert mr._retry_after_seconds(_http_error(429, "not-a-number")) is None


def test_rate_limit_delay_prefers_header_then_exponential_cap():
    assert mr._rate_limit_delay(0, _http_error(429, "7")) == 7.0
    # No header: 10s, 20s, 40s ... capped at 60s.
    bare = _http_error(429)
    assert mr._rate_limit_delay(0, bare) == 10.0
    assert mr._rate_limit_delay(1, bare) == 20.0
    assert mr._rate_limit_delay(2, bare) == 40.0
    assert mr._rate_limit_delay(5, bare) == 60.0


def test_chat_persistent_429_raises_typed_rate_limit_with_backoff(monkeypatch):
    import urllib.request
    sleeps = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(429)))
    client = mr.MinimaxClient.__new__(mr.MinimaxClient)  # skip key-file read
    client.base, client.model, client.key = "http://proxy", "MiniMax-M3", "k"
    with pytest.raises(mr.RateLimitError):
        client.chat([{"role": "user", "content": "x"}],
                    retries=2, sleeper=sleeps.append)
    # attempt 0 -> 10s, attempt 1 -> 20s, then attempt 2 gives up without sleeping.
    assert sleeps == [10.0, 20.0]


def test_chat_retry_after_overrides_exponential_sleep(monkeypatch):
    import urllib.request
    sleeps = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(429, "5")))
    client = mr.MinimaxClient.__new__(mr.MinimaxClient)
    client.base, client.model, client.key = "http://proxy", "MiniMax-M3", "k"
    with pytest.raises(mr.RateLimitError):
        client.chat([{"role": "user", "content": "x"}],
                    retries=1, sleeper=sleeps.append)
    assert sleeps == [5.0]


def test_chat_non_429_http_error_stays_generic(monkeypatch):
    import urllib.request
    sleeps = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(500)))
    client = mr.MinimaxClient.__new__(mr.MinimaxClient)
    client.base, client.model, client.key = "http://proxy", "MiniMax-M3", "k"
    with pytest.raises(RuntimeError) as ei:
        client.chat([{"role": "user", "content": "x"}],
                    retries=2, sleeper=sleeps.append)
    assert not isinstance(ei.value, mr.RateLimitError)
    assert sleeps == [2.0, 4.0]  # generic short backoff, not the 429 schedule


def test_circuit_opens_for_cooldown_then_closes_with_injected_clock():
    now = {"t": 1000.0}
    circuit = mr.RateLimitCircuit(cooldown_seconds=180, clock=lambda: now["t"])
    assert circuit.is_open() is False
    circuit.trip()
    assert circuit.trips == 1 and circuit.is_open() is True
    assert circuit.remaining() == 180.0
    now["t"] += 179
    assert circuit.is_open() is True
    now["t"] += 2  # past the window
    assert circuit.is_open() is False
    # An explicit retry-after overrides the default cooldown.
    circuit.trip(retry_after=30)
    assert circuit.remaining() == 30.0 and circuit.is_open() is True


def _make_review_task(tid, protocol):
    role_id = "A3-backend-engineer"
    protocol.register_role(role_id)
    protocol.create_task(tid, title="t", description="d", role_id=role_id,
                         acceptance_criteria="c")
    protocol.ready_task(tid)
    protocol.atomic_claim(tid, agent_id="doubao-owner", executor_id="doubao")
    protocol.transition(tid, "IN_PROGRESS", "doubao-owner", "work")
    protocol.transition(tid, "SUBMITTED", "doubao-owner", "done")
    protocol.transition(tid, "REVIEW", "doubao-owner", "submit")


class _CountingStubClient:
    def __init__(self):
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        return json.dumps({"verdict": "ACCEPTED", "confidence": 0.9,
                           "reasons": [], "blocking_issues": ["note"]})


class _RateLimitedClient:
    def __init__(self):
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        raise mr.RateLimitError("HTTP 429 after retries")


def test_review_once_serializes_to_one_task_per_tick(db_path, monkeypatch):
    import protocol
    protocol.init_db()
    _make_review_task("T-rl-a", protocol)
    _make_review_task("T-rl-b", protocol)
    monkeypatch.setattr(mr, "_gather_evidence",
                        lambda task: {"head": None, "changed": [], "diff_stat": "", "diff": "",
                                      "file_contents": "", "checks": "", "has_runnable_check": False})
    monkeypatch.setattr(mr, "_update_bridge_card", lambda t, stage, reason: None)

    client = _CountingStubClient()
    outcomes = mr.review_once(client)  # default limit=1
    assert len(outcomes) == 1 and outcomes[0]["task_id"] == "T-rl-a"
    assert client.calls == 1  # the second task is NOT offered to the model this tick
    assert protocol.get_task("T-rl-a")["status"] == "ACCEPTED"
    assert protocol.get_task("T-rl-b")["status"] == "REVIEW"


def test_review_once_429_breaks_batch_and_leaves_tasks_in_review(db_path, monkeypatch):
    import protocol
    protocol.init_db()
    _make_review_task("T-rl-c", protocol)
    _make_review_task("T-rl-d", protocol)
    monkeypatch.setattr(mr, "_gather_evidence",
                        lambda task: {"head": None, "changed": [], "diff_stat": "", "diff": "",
                                      "file_contents": "", "checks": "", "has_runnable_check": False})

    client = _RateLimitedClient()
    outcomes = mr.review_once(client, limit=5)  # even a high limit must stop at the first 429
    assert len(outcomes) == 1
    assert outcomes[0]["task_id"] == "T-rl-c"
    assert outcomes[0]["verdict"] == "RATE_LIMITED"
    assert client.calls == 1  # circuit: the second task never reached the saturated endpoint
    # No state transition on a rate limit: both remain REVIEW for a later tick.
    assert protocol.get_task("T-rl-c")["status"] == "REVIEW"
    assert protocol.get_task("T-rl-d")["status"] == "REVIEW"


def test_worktree_lookup_prefers_the_recorded_path(tmp_path, monkeypatch):
    # The platform's own executors create worktrees next to the repository
    # (protocol.create_worktree -> <repo>.parent/wt-<id>). Guessing by naming
    # convention made the reviewer look at a non-existent directory and judge
    # the task as "no implementation" (false REWORK, 2026-11-20).
    recorded = tmp_path / "wt-recorded"
    recorded.mkdir()
    monkeypatch.setattr(mr, "WORKTREE_ROOT", tmp_path / "convention-root")
    task = {"task_id": "T-900", "worktree_path": str(recorded)}
    assert mr._recorded_worktree(task) == recorded
    # Without a task (legacy call) the convention fallback still applies.
    convention = tmp_path / "convention-root" / "T-901-doubao"
    convention.parent.mkdir(parents=True)
    convention.mkdir()
    assert mr._worktree_for("T-901") == convention


def test_worktree_lookup_falls_back_when_recorded_path_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "WORKTREE_ROOT", tmp_path)
    task = {"task_id": "T-902", "worktree_path": str(tmp_path / "missing")}
    assert mr._recorded_worktree(task) is None
