#!/usr/bin/env python3
"""Minimax-powered acceptance + integration worker (independent reviewer node).

A long-running controller-side process that uses MiniMax-M3 (through the local
CC Switch OpenAI-compatible proxy) as an *independent* reviewer, so the agent
that implemented a task never accepts its own work.

acceptance node:
  scans console tasks in REVIEW, gathers the worktree diff + acceptance
  criteria + evidence, asks MiniMax for a structured ACCEPTED/REWORK verdict,
  and writes it back through the protocol state machine.

integration node:
  for ACCEPTED tasks, runs a strictly read-only mergeability analysis
  (git merge-tree) and parks the result; the final master merge stays a human
  action because the main repo working tree carries pre-existing uncommitted
  changes that make an automated checkout/merge unsafe.

Pure stdlib (urllib). One bad task is contained and never kills the loop.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import protocol

PROXY_BASE = "http://127.0.0.1:15721/v1"
MODEL = "MiniMax-M3"
KEY_FILE = Path(os.environ.get("DSH_MINIMAX_KEY_FILE", r"C:/Users/32831/OneDrive/Desktop/期货分析/.claude/.env.minimax"))
WORKTREE_ROOT = Path(os.environ.get("DSH_WT_ROOT", r"C:/Users/32831/wt"))
REPO = os.environ.get("DSH_REPO", r"C:/Users/32831/OneDrive/Desktop/期货分析")
BRIDGE_ROOT = Path(os.environ.get("DSH_BRIDGE_ROOT", r"C:/Users/32831/wt/bridge-live"))
REVIEW_ACTOR = "minimax-reviewer"
# Input-side evidence budget (the model's output stays capped at 2500 tokens).
# A task's primary source file can be large (e.g. an 185KB ECharts component);
# skipping it wholesale starved the reviewer into a "cannot verify" REWORK, so
# oversized files now contribute a bounded head slice instead of nothing.
MAX_DIFF_CHARS = 40000
MAX_FILE_CHARS = 48000
MAX_TOTAL_FILE_CHARS = 120000
_TEXT_SUFFIX = (".py", ".mjs", ".js", ".ts", ".vue", ".md", ".json", ".txt")
# Untracked controller/harness artifacts written into every worktree. They are
# never committed and never appear in the reviewer's committed-diff list, but a
# worker's own `git status` evidence can list them, so the prompt must not let
# them be mistaken for out-of-whitelist implementer changes.
HARNESS_ARTIFACT_MARKERS = ("DISPATCH-", "logs/", "BRIDGE_AUTH.md")


class RateLimitError(RuntimeError):
    """Upstream MiniMax Token Plan returned HTTP 429 after the in-call retries.

    Distinct from a generic failure so the review loop can open a circuit
    breaker instead of replaying every REVIEW task into the same saturated
    concurrency window (CC Switch upstream code 2056).
    """


def _retry_after_seconds(exc: urllib.error.HTTPError) -> Optional[float]:
    """Parse a Retry-After header (delta seconds or HTTP-date); None if absent."""
    if not getattr(exc, "headers", None):
        return None
    value = exc.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:  # RFC 7231 also allows an HTTP-date.
            from email.utils import parsedate_to_datetime
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def _rate_limit_delay(attempt: int, exc: Exception) -> float:
    """Seconds to wait before retrying a 429 (pure: no sleep, easy to test)."""
    if isinstance(exc, urllib.error.HTTPError):
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            return retry_after
    # Exponential 10s, 20s, 40s ... capped at 60s. The old fixed 2s/4s backoff
    # was far shorter than the upstream window and kept the limit tripped.
    return min(60.0, 10.0 * (2 ** max(0, attempt)))


class MinimaxClient:
    def __init__(self, base: str = PROXY_BASE, model: str = MODEL, key_file: Path = KEY_FILE) -> None:
        self.base = base.rstrip("/")
        self.model = model
        self.key = key_file.read_text(encoding="utf-8").strip()

    def chat(self, messages: List[Dict[str, str]], *, temperature: float = 0.1,
             max_tokens: int = 2500, retries: int = 2,
             sleeper: Callable[[float], None] = time.sleep) -> str:
        # Disable MiniMax-M3's long <think> chain: the proxy honours this and
        # returns a short, direct JSON answer instead of a 20k-char reasoning
        # block that truncated the verdict under any smaller token budget.
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens,
                   "thinking": {"type": "disabled"}}
        body = json.dumps(payload).encode("utf-8")
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(
                    f"{self.base}/chat/completions", data=body,
                    headers={"Authorization": f"Bearer {self.key}",
                             "Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                choices = data.get("choices")
                if not choices or not choices[0].get("message"):
                    raise RuntimeError(f"unexpected minimax response shape: {str(data)[:200]}")
                content = choices[0]["message"].get("content")
                if not content:
                    raise RuntimeError("minimax returned empty content")
                return content
            except urllib.error.HTTPError as exc:
                # HTTPError is a URLError subclass, so it must be handled first.
                last_err = exc
                if attempt >= retries:
                    break
                if exc.code == 429:
                    # Token Plan concurrency cap (CC Switch upstream 2056). Back
                    # off far longer than the old fixed 2s/4s, honour Retry-After
                    # when present, and let the loop surface a typed error below.
                    sleeper(_rate_limit_delay(attempt, exc))
                else:
                    sleeper(min(30.0, 2.0 * (attempt + 1)))
            except (urllib.error.URLError, KeyError, TimeoutError) as exc:
                last_err = exc
                if attempt >= retries:
                    break
                sleeper(2.0 * (attempt + 1))
        if isinstance(last_err, urllib.error.HTTPError) and last_err.code == 429:
            raise RateLimitError(
                f"minimax rate-limited (HTTP 429) after {retries + 1} attempts: {last_err}")
        raise RuntimeError(f"minimax call failed after retries: {last_err}")


def _git(worktree: str, *args: str) -> subprocess.CompletedProcess:
    # Force UTF-8: Windows defaults to GBK and crashes on Chinese file contents.
    return subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _worktree_for(tid: str) -> Optional[Path]:
    for suffix in ("-doubao", ""):
        candidate = WORKTREE_ROOT / f"{tid}{suffix}"
        if candidate.is_dir():
            return candidate
    return None


def _gather_evidence(task: Dict[str, Any]) -> Dict[str, Any]:
    tid = task["task_id"]
    wt = _worktree_for(tid)
    evidence: Dict[str, Any] = {"worktree": str(wt) if wt else None,
                                "head": None, "diff_stat": "", "diff": "", "changed": [],
                                "file_contents": "", "checks": "", "has_runnable_check": False}
    evidence["has_runnable_check"] = _has_runnable_check(task)
    if not wt:
        return evidence
    head = _git(str(wt), "rev-parse", "HEAD")
    evidence["head"] = head.stdout.strip() if head.returncode == 0 else None
    stat = _git(str(wt), "diff", "master...HEAD", "--stat")
    evidence["diff_stat"] = stat.stdout.strip()
    changed = _git(str(wt), "diff", "master...HEAD", "--name-only")
    evidence["changed"] = [l for l in changed.stdout.splitlines() if l.strip()]
    diff = _git(str(wt), "diff", "master...HEAD")
    evidence["diff"] = diff.stdout[:MAX_DIFF_CHARS]
    # Full contents of the whitelisted text files, so the reviewer can verify
    # the whole implementation rather than a truncated diff. Collected
    # smallest-first: a 185KB core component must not crowd out the small
    # type-definition files next to it (a reviewer that never saw a file
    # cannot judge it), and every skipped file is named explicitly instead of
    # silently vanishing behind the budget.
    candidates: List[tuple] = []
    for rel in evidence["changed"]:
        if not rel.endswith(_TEXT_SUFFIX):
            continue
        path = wt / rel
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        candidates.append((len(content), rel, content))
    blobs, total, skipped = [], 0, []
    for _size, rel, content in sorted(candidates, key=lambda item: item[0]):
        if len(content) > MAX_FILE_CHARS:
            # Keep a substantive head slice rather than hiding the core file;
            # the full committed change for this file is still in evidence["diff"].
            content = (content[:MAX_FILE_CHARS]
                       + f"\n\n……[文件 {len(content)} 字符超出单文件预算，仅提供前 {MAX_FILE_CHARS} 字符；"
                         "完整改动请看上方 diff]……")
        if total + len(content) > MAX_TOTAL_FILE_CHARS:
            skipped.append(rel)
            continue
        blobs.append(f"\n\n===== {rel} =====\n{content}")
        total += len(content)
    if skipped:
        blobs.append("\n\n===== 以下文件因证据预算未提供完整内容（不要据此判断其内容，"
                     "请以上方 diff 为准）=====\n"
                     + "\n".join(f"  - {rel}" for rel in skipped))
    evidence["file_contents"] = "".join(blobs)
    checks = _run_behavioral_checks(task, wt)
    auto = _run_whitelisted_tests(task, wt)
    if auto:
        evidence["has_runnable_check"] = True
    evidence["checks"] = f"{checks}\n\n{auto}".strip() if checks else auto
    return evidence


# Read-only verification commands that may run inside a task worktree. The
# check (after stripping a leading `cd ... &&`) must match one of these.
_CHECK_RE = re.compile(
    r'^("?[A-Za-z]:[/\\][^"]*python\.exe"?|python(?:\s+-m\s+pytest)?|pytest|node\b|npx\s+(?:vue-)?tsc)\b',
    re.IGNORECASE,
)
# Hard block: even if a check line otherwise matches, refuse anything that can
# mutate state or reach the network.
_FORBIDDEN_TOKENS = ("rm ", "del ", "curl ", "wget ", "git push", "git merge", "git reset",
                     "pip install", "mv ", "sudo", "remove-item", "invoke-webrequest", ">", ">>")
_VENV_PY = os.environ.get("DSH_VENV_PY", r"C:/Users/32831/openmontage/.venv/Scripts/python.exe")


def _resolve_command(cmd: str) -> str:
    # A full interpreter path is already correct; only rewrite bare `python`.
    if re.match(r"^[A-Za-z]:[/\\]", cmd):
        return cmd
    if cmd.startswith("python -m pytest"):
        return f'"{_VENV_PY}" -m pytest' + cmd[len("python -m pytest"):]
    if cmd.startswith("pytest"):
        return f'"{_VENV_PY}" -m pytest' + cmd[len("pytest"):]
    if cmd.startswith("python "):
        return f'"{_VENV_PY}" ' + cmd[len("python "):]
    return cmd


# Allow-listed check commands are pure ASCII and never contain a CJK character
# or an ASCII arrow. Either marks the start of human-readable expected-result
# prose glued onto the command, e.g. "npx vue-tsc --noEmit 退出码 0。" or
# "python EV.py -> pass=4 fail=0 (rc=0)". It must be cut before execution:
# vue-tsc otherwise compiles the prose tokens as files (TS6231), and the ">"
# inside "->" trips the forbidden-token guard so a real check is skipped.
_PROSE_RE = re.compile(r"[一-鿿　-〿＀-￯]|->")


def _strip_inline_prose(line: str) -> str:
    """Drop expected-result prose glued onto a command (CJK clause or ``->``)."""
    match = _PROSE_RE.search(line)
    return line[:match.start()].rstrip() if match else line.rstrip()


def _has_runnable_check(task: Dict[str, Any]) -> bool:
    """True if behavioral_check contains an allow-listed command after stripping.

    Pure-prose acceptance criteria return False so the reviewer treats the
    absence of an exit code as a spec property (no automatable check), not as a
    failed or missing run that should by itself force REWORK.
    """
    raw = (task.get("behavioral_check") or "").strip()
    for segment in re.split(r"[；;\n]", raw):
        segment = _strip_inline_prose(segment.strip())
        if not segment:
            continue
        check_part = re.sub(r"^cd\s+[^\s&]+\s*&&\s*", "", segment)
        if _CHECK_RE.match(check_part) and not _is_bare_pytest(check_part):
            return True
    return False


def _hermetic_check_env() -> Dict[str, str]:
    """Environment for check subprocesses that makes pytest runs deterministic.

    Two machine-specific traps otherwise make every pytest-based check in the
    target repo fail before a single assertion runs:

    * the repo ``pytest.ini`` addopts request ``--html`` / ``--self-contained-html``
      (pytest-html), which is absent from the review venv, so pytest exits RC=4
      (usage error) and zero tests execute;
    * the shared ``%TEMP%\\pytest-of-<user>`` basetemp is owned by another
      account and raises ``PermissionError`` as soon as a test touches tmp_path.

    ``PYTEST_ADDOPTS`` is inherited even by pytest nested inside a wrapper
    verify script, and is simply ignored by node/npx. Clearing addopts also
    cancels the html-report write, keeping the check strictly read-only.

    The basetemp is a stable reused path (not a random mkdtemp): some target
    tests stringify tmp_path into a cwd-relative scratch filename, so a random
    suffix would leave a new untracked litter folder in the worktree on every
    tick; a stable name makes those writes overwrite instead of accumulate.
    """
    basetemp = Path(tempfile.gettempdir()) / "ccs-review-basetemp"
    basetemp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTEST_ADDOPTS"] = f"-o addopts= --basetemp {basetemp}"
    return env


def _is_bare_pytest(cmd: str) -> bool:
    """True for a pytest invocation that names no test path.

    A bare ``pytest`` runs the whole suite, so its exit code reflects
    unrelated files, the repo's html-report addopts and machine load rather
    than this task. Prose like "pytest 覆盖 quadrant 标准门必须全绿" also
    matched the allow-list as a command; running it only produced noise.
    """
    if not re.match(r"^(python\s+-m\s+pytest|pytest)\b", cmd.strip(), re.IGNORECASE):
        return False
    return not re.search(r"[\w./\\-]+\.py\b", cmd)


def _auto_test_files(task: Dict[str, Any], wt: Path) -> List[str]:
    """Whitelisted pytest files that belong to this task.

    Many acceptance criteria are written as prose with no executable command,
    which left the reviewer to run pytest itself -- and to trip over the repo's
    html-report addopts and the shared, unwritable pytest basetemp. The
    whitelisted test files of the task are run here instead, hermetically.
    """
    allowed = json.loads(task.get("allowed_paths_json") or "[]")
    found = []
    for rel in allowed:
        normalized = rel.replace("\\", "/")
        if not normalized.endswith(".py"):
            continue
        if "/tests/" not in normalized and not normalized.startswith("tests/"):
            continue
        if not Path(normalized).name.startswith("test_"):
            continue
        if (wt / rel).is_file():
            found.append(rel)
    return found


def _run_whitelisted_tests(task: Dict[str, Any], wt: Path) -> str:
    """Run the task's whitelisted pytest files hermetically; report exit codes."""
    files = _auto_test_files(task, wt)
    if not files:
        return ""
    basetemp = Path(tempfile.gettempdir()) / f"ccs-review-tests-{task['task_id']}"
    basetemp.mkdir(parents=True, exist_ok=True)
    cmd = (f'"{_VENV_PY}" -m pytest -q -o addopts= --basetemp "{basetemp}" '
           + " ".join(f'"{f}"' for f in files))
    try:
        proc = subprocess.run(cmd, cwd=str(wt), shell=True, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=600, env=_hermetic_check_env())
    except Exception as exc:  # noqa: BLE001
        return f"$ [自动] 白名单测试 {files}\n[check-error] {type(exc).__name__}: {exc}"
    tail = (proc.stdout + proc.stderr)[-2500:]
    return (f"$ [自动] 白名单测试 hermetic 重跑: {files}\n"
            f"[exit={proc.returncode}]\n{tail}")


def _run_behavioral_checks(task: Dict[str, Any], wt: Path) -> str:
    """Re-run declared read-only checks in the worktree, keeping `cd &&` semantics.

    Splits on ASCII/Chinese semicolons and newlines (a Chinese clause is usually
    prose, not a command), strips expected-result prose glued onto each command
    (a CJK clause or an ASCII ``->`` arrow), validates each check against an
    allow-list (full venv python paths included), blocks mutating/network tokens,
    then runs the cleaned shell line so relative `cd` works and prose is never
    passed as argv. Pytest runs get a hermetic PYTEST_ADDOPTS (see
    ``_hermetic_check_env``) so missing plugins / a broken shared basetemp can
    never turn an environment fault into a false task failure.
    """
    raw = (task.get("behavioral_check") or "").strip()
    if not raw:
        return ""
    output, budget = [], 3
    check_env = _hermetic_check_env()
    for line in re.split(r"[；;\n]", raw):
        line = _strip_inline_prose(line.strip())
        if not line or budget <= 0:
            continue
        lowered = line.lower()
        if any(tok in lowered for tok in _FORBIDDEN_TOKENS):
            output.append(f"$ {line}\n[skipped: forbidden token]")
            continue
        check_part = re.sub(r"^cd\s+[^\s&]+\s*&&\s*", "", line)
        if not _CHECK_RE.match(check_part):
            continue
        if _is_bare_pytest(check_part):
            continue  # a whole-suite run says nothing about this task
        budget -= 1
        safe_line = _resolve_command(line)
        try:
            proc = subprocess.run(safe_line, cwd=str(wt), shell=True, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=300,
                                  env=check_env)
            tail = (proc.stdout + proc.stderr)[-1800:]
            output.append(f"$ {safe_line}\n[exit={proc.returncode}]\n{tail}")
        except Exception as exc:  # noqa: BLE001
            output.append(f"$ {safe_line}\n[check-error] {type(exc).__name__}: {exc}")
    return "\n\n".join(output)


def _normalize_verdict(raw: Any) -> str:
    """Map a free-form model verdict token to the three console decisions.

    NEEDS_HUMAN is a park-for-human decision (human-only action remains, or a
    visual/behavioural criterion needs a human sign-off), distinct from both
    ACCEPTED (fully verified) and REWORK (the implementer must change code).
    """
    text = str(raw or "").upper().replace(" ", "_")
    if "NEEDS" in text or "HUMAN" in text:
        return "NEEDS_HUMAN"
    if text.startswith("ACCEPT"):
        return "ACCEPTED"
    return "REWORK"


def _parse_verdict(text: str) -> Dict[str, Any]:
    """Extract a JSON verdict from a chain-of-thought model reply.

    MiniMax-M3 emits a ``<think>..</think>`` reasoning block before the answer;
    strip it and any markdown fence first, then locate the JSON object. If the
    JSON is malformed, fall back to a regex on the verdict field so a formatting
    slip never blocks the pipeline.
    """
    import re
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            verdict = json.loads(cleaned[start:end + 1])
            verdict["verdict"] = _normalize_verdict(verdict.get("verdict", ""))
            verdict.setdefault("reasons", [])
            verdict.setdefault("blocking_issues", [])
            return verdict
        except json.JSONDecodeError:
            pass
    # Robust fallback: recover at least the decision.
    match = re.search(
        r'"?verdict"?\s*:\s*"?(ACCEPTED|NEEDS[_\s]HUMAN|REWORK|ACCEPT|REJECT)',
        cleaned, re.IGNORECASE)
    if not match:
        raise ValueError("no parseable verdict in model reply")
    decision = _normalize_verdict(match.group(1))
    return {"verdict": decision, "confidence": 0.5, "reasons": [],
            "blocking_issues": ["模型 JSON 格式异常，按关键词解析判定，建议人工复核"]}


def _build_review_prompt(task: Dict[str, Any], ev: Dict[str, Any]) -> List[Dict[str, str]]:
    allowed = json.loads(task.get("allowed_paths_json") or "[]")
    system = (
        "你是独立的代码验收员，不参与实现，也不采信实现者的自我陈述。"
        "只依据验收标准、白名单和真实 git diff 独立判断。严格只输出一个 JSON 对象，"
        '格式 {"verdict":"ACCEPTED|NEEDS_HUMAN|REWORK","confidence":0到1的数,"reasons":[...],'
        '"blocking_issues":[...]}，不要输出 JSON 以外的任何文字。'
    )
    if ev.get("has_runnable_check"):
        check_block = ev.get("checks") or "（声明了可执行检查但未产出结果，按证据不足处理）"
        check_rule = "③客观检查 exit code 是否为 0（非 0 即不通过）；"
    else:
        check_block = (
            ev.get("checks")
            or "（本任务 behavioral_check 是人工验收描述，不含可执行命令；缺少 exit code 是规格本身"
               "属性而非检查失败，不得仅凭“无 exit code”判 REWORK；请依据白名单合规性、完整文件内容"
               "与 diff，逐条核对每条行为验收描述是否被真实实现。）")
        check_rule = "③本任务无自动化检查，核对文件实现是否真实满足每条行为验收描述；"
    user = f"""任务 {task['task_id']}：{task.get('title','')}

## 验收标准（pass_condition）
{task.get('pass_condition') or '（未提供）'}

## 行为检查（behavioral_check）
{task.get('behavioral_check') or '（未提供）'}

## 允许改动的白名单
{json.dumps(allowed, ensure_ascii=False)}

## 实际改动文件（已提交 diff 的权威清单，白名单合规只以此为准）
{json.dumps(ev.get('changed', []), ensure_ascii=False)}

注：DISPATCH-*.json、logs/、BRIDGE_AUTH.md 等是控制器/harness 生成的**未跟踪**文件，不属于
实现者的提交改动；若实现者自带的 git status 证据里出现它们，不得据此判越界。越界判定只看上面
这份已提交清单里是否存在白名单之外的真实文件。

## diff --stat
{ev.get('diff_stat','（无 worktree）')}

## diff（可能被截断）
{ev.get('diff','（无 worktree）')}

## 白名单文件完整内容（超大文件给出有界头部片段，完整改动见上方 diff）
{ev.get('file_contents') or '（无文本文件或超出预算）'}

## 验收方独立重跑行为检查的客观结果（exit code 为准）
{check_block}

## 三态判定口径（严格据此选择 verdict）
- ACCEPTED：每条可核验标准都满足、客观检查 exit 0、无越界、证据真实自洽、且没有遗留待人工动作。
- NEEDS_HUMAN：自动化/客观检查与结构证据都通过，但剩下的环节只能由人工完成或确认，典型两种——
  ①pass_condition 明确点名的人工动作（如凭据轮换、人工签字），实现者被禁止自动执行；
  ②无自动化检查的视觉/无障碍/交互行为，结构实现与同仓测试一致且完整，但最终需人眼或真实浏览器签收。
  NEEDS_HUMAN 不是失败：不要就实现者无权做的人工项要求其返工。
- REWORK：仅用于实现者能且必须修的问题——客观检查非 0、白名单越界、规格元素确实缺失、实现与证据矛盾或证据造假。

## 本机 pytest 调用纪律（自行跑测试时必须遵守，否则结论无效）
- 必须写成：`python -m pytest -o addopts= --basetemp <你新建的可写私有目录> -q <测试路径>`。
  直接 `pytest` 会因仓库 pytest.ini 注入 `--html`（本机 venv 未装 pytest-html）报 RC=4 用法错误，
  且共享临时目录 pytest-of-<user> 可能无权限而在 setup 阶段 PermissionError——**这两种都是环境假象，不是任务失败**。
- **必须做基线对比**：与本次改动无关的既有失败（master 上同样失败）不构成本任务的缺陷；
  请先确认该失败在 master 基线上是否同样存在，再决定是否归因于本任务。
- 前端类型检查：`npx vue-tsc --noEmit` 需在装有 vue-tsc 的 worktree 执行；若该 worktree 的 node_modules 缺 vue-tsc，
  请在装有它的 worktree 验证或如实说明无法验证，不要臆测。
- 仓库既有失败（如 token 契约门 test_quadrant_ring_anatomy）不是本次改动的责任，除非本次改动正是引入它们的那一处。

请核对：①是否满足每条验收标准；②仅按上面的已提交清单核对是否越界（harness 未跟踪产物不算）；{check_rule}④证据是否真实自洽。
再按上面的三态口径选择 verdict，并在 blocking_issues 写明依据。"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _bridge_stage_sequence(current: str, target: str) -> List[str]:
    """Legal bridge-card stage hops from ``current`` to ``target``.

    agent_bridge only allows ``retry_requested`` from in_progress /
    awaiting_review / auditing / failed. A card still sitting at an early stage
    (dispatched / claimed / needs_human) cannot jump there directly, so route
    it through in_progress instead of raising "illegal stage transition".
    """
    if current == target:
        return []
    if target == "retry_requested" and current in ("dispatched", "claimed", "needs_human"):
        return ["in_progress", "retry_requested"]
    return [target]


def _update_bridge_card(tid: str, stage: str, reason: str) -> Optional[str]:
    """Mirror a console verdict onto the bridge responsibility card.

    REWORK -> retry_requested (the Doubao owner can re-claim and fix);
    ACCEPTED/NEEDS_HUMAN -> needs_human (awaiting a human action/integration).
    Returns an error string on failure, None on success. Never raises.

    A task implemented by a CLI/headless worker (not the Doubao mailbox) has no
    responsibility card: its absence is a normal no-op, not a sync failure.
    """
    try:
        pkg = os.environ.get("DSH_BRIDGE_PKG", "C:/Users/32831/wt/agent-bridge-implementation/.claude")
        if pkg not in sys.path:
            sys.path.insert(0, pkg)
        from agent_bridge.notifications import emit
        from agent_bridge.responsibility import _changed, utc_now
        from agent_bridge.store import BridgeStore
        store = BridgeStore(BRIDGE_ROOT)
        try:
            card = store.load_task(tid)
        except FileNotFoundError:
            # No bridge card: this task never went through the Doubao mailbox,
            # so there is nothing to mirror. The console verdict still stands.
            return None
        run_id = card["execution"]["run_id"]
        owner = card["execution"]["owner"]
        for hop in _bridge_stage_sequence(card["execution"]["status"], stage):
            card = _changed(card, hop, REVIEW_ACTOR, f"minimax-{tid}", utc_now)
            store.update_task(tid, lambda _c, _card=card: _card)
        emit(BRIDGE_ROOT, task_id=tid, run_id=run_id,
             owner=owner, card_status=stage,
             kind=stage, detail={"reason": reason[:500]})
        return None
    except Exception as exc:  # noqa: BLE001 - console verdict still stands
        return f"{type(exc).__name__}: {exc}"


class RateLimitCircuit:
    """Cross-tick cooldown: once a 429 is seen, make zero LLM calls until the
    window passes. Without this the daemon retried every ``--interval`` seconds
    against an account whose Token Plan concurrency was already saturated,
    sustaining the very limit (upstream code 2056) that blocked acceptance.

    Time is injected so the logic is hermetic (no real ``sleep`` in tests).
    """

    def __init__(self, cooldown_seconds: float = 180.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self.open_until = 0.0
        self.trips = 0

    def is_open(self) -> bool:
        return self._clock() < self.open_until

    def remaining(self) -> float:
        return max(0.0, self.open_until - self._clock())

    def trip(self, retry_after: Optional[float] = None) -> None:
        self.trips += 1
        delay = retry_after if (retry_after is not None and retry_after > 0) \
            else self.cooldown_seconds
        self.open_until = self._clock() + float(delay)


def review_once(client: MinimaxClient, *, limit: int = 1) -> List[Dict[str, Any]]:
    """Review up to ``limit`` REVIEW tasks (serialized by default) and return
    per-task outcomes.

    A :class:`RateLimitError` opens the circuit for this tick: the failing task
    is recorded as ``RATE_LIMITED`` (it stays REVIEW and is retried later) and
    no further task is offered to the model, so one saturated window cannot be
    hit by the whole REVIEW backlog in a single tick.
    """
    outcomes = []
    with protocol.get_db() as conn:
        review_tasks = [dict(r) for r in conn.execute(
            "SELECT * FROM tasks WHERE status='REVIEW' ORDER BY task_id")]
    for task in review_tasks[:max(1, limit)]:
        tid = task["task_id"]
        try:
            ev = _gather_evidence(task)
            raw = client.chat(_build_review_prompt(task, ev))
            verdict = _parse_verdict(raw)
            reason = f"minimax {verdict['verdict']} (conf={verdict.get('confidence')}): " \
                     f"{'; '.join(verdict.get('blocking_issues') or verdict.get('reasons') or [''])}"[:400]
            bridge_err = None
            decision = verdict["verdict"]
            if decision == "ACCEPTED":
                protocol.transition(tid, "ACCEPTED", REVIEW_ACTOR, reason)
                if ev["head"]:
                    protocol.add_evidence(tid, "commit_sha", f"commit:{ev['head']}",
                                          REVIEW_ACTOR, sha256=ev["head"], sync_audit=False)
                # Fully verified: park the card for the human integration gate.
                bridge_err = _update_bridge_card(tid, "needs_human", reason)
            elif decision == "NEEDS_HUMAN":
                # Objective/structural evidence passes; only a human-only action
                # or human visual/behavioural sign-off remains. Not a bounce to
                # the implementer (it is forbidden from doing the human part).
                protocol.transition(tid, "NEEDS_HUMAN", REVIEW_ACTOR, reason)
                if ev["head"]:
                    protocol.add_evidence(tid, "commit_sha", f"commit:{ev['head']}",
                                          REVIEW_ACTOR, sha256=ev["head"], sync_audit=False)
                bridge_err = _update_bridge_card(tid, "needs_human", reason)
            else:
                protocol.transition(tid, "REWORK", REVIEW_ACTOR, reason)
                bridge_err = _update_bridge_card(tid, "retry_requested", reason)
            outcomes.append({"task_id": tid, "verdict": decision,
                             "bridge_sync": bridge_err or "ok"})
        except RateLimitError as exc:
            # Circuit: stop this tick immediately; the task remains REVIEW.
            outcomes.append({"task_id": tid, "verdict": "RATE_LIMITED",
                             "error": f"{type(exc).__name__}: {exc}"})
            break
        except Exception as exc:  # noqa: BLE001 - contain and keep the loop alive
            outcomes.append({"task_id": tid, "verdict": "ERROR", "error": f"{type(exc).__name__}: {exc}"})
    return outcomes


def integration_preview() -> List[Dict[str, Any]]:
    """Read-only mergeability analysis for ACCEPTED tasks (no checkout/merge/push)."""
    reports = []
    with protocol.get_db() as conn:
        accepted = [r[0] for r in conn.execute(
            "SELECT task_id FROM tasks WHERE status='ACCEPTED' ORDER BY task_id")]
    for tid in accepted:
        wt = _worktree_for(tid)
        if not wt:
            reports.append({"task_id": tid, "verdict": "no-worktree"})
            continue
        branch = _git(str(wt), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        check = subprocess.run(
            ["git", "-C", REPO, "merge-tree", "master", branch],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        conflict_markers = [ln for ln in check.stdout.splitlines() if ln.startswith(("<<<<<<<", "changed in both"))]
        reports.append({
            "task_id": tid, "branch": branch,
            "mergeable": check.returncode == 0 and not conflict_markers,
            "conflict": bool(conflict_markers),
            "note": "只读预检通过；主仓工作区有未提交改动，最终合 master 由人工执行，不自动 push。",
        })
    return reports


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MiniMax acceptance/integration worker")
    parser.add_argument("--once", action="store_true", help="one tick then exit")
    parser.add_argument("--interval", type=float, default=45.0)
    parser.add_argument("--preview-only", action="store_true", help="only run integration previews")
    parser.add_argument("--limit", type=int, default=1,
                        help="max reviews per tick; serialized to 1 by default to stay under "
                             "the MiniMax Token Plan concurrency cap (upstream 2056)")
    parser.add_argument("--cooldown", type=float, default=180.0,
                        help="seconds to hold the circuit open (zero LLM calls) after a 429")
    args = parser.parse_args(argv)
    client = MinimaxClient()
    circuit = RateLimitCircuit(cooldown_seconds=args.cooldown)

    def tick():
        if args.preview_only:
            return {"integration_preview": integration_preview()}
        # Open circuit: skip every LLM call this tick. The read-only git
        # integration preview still runs because it makes no model request.
        if circuit.is_open():
            return {"reviews": [], "circuit": "open",
                    "retry_after_seconds": round(circuit.remaining(), 1),
                    "integration_preview": integration_preview()}
        outcomes = review_once(client, limit=args.limit)
        if any(o.get("verdict") == "RATE_LIMITED" for o in outcomes):
            circuit.trip()
        return {"reviews": outcomes, "integration_preview": integration_preview(),
                "circuit_trips": circuit.trips}

    if args.once:
        print(json.dumps(tick(), ensure_ascii=False, indent=2))
        return 0
    print(f"minimax reviewer loop started (model={MODEL}, interval={args.interval}s, "
          f"limit={args.limit}/tick, cooldown={args.cooldown}s)", flush=True)
    while True:
        try:
            print(json.dumps(tick(), ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"fatal": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
