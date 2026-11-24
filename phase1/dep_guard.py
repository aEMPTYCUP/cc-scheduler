"""派单前**依赖检测**：把「有依赖的任务并行派发」这类错误挡在派单之前。

背景（同一个错误犯了两次）
--------------------------
1. 阶段 5：T-ALERT-007（引擎）与 T-ALERT-008（服务接口 + 真实样本）**有依赖**
   —— 008 要调 007 的 auto_release.py、要跑真实流程产出样本。
   我并行派了，且 008 的 worktree 基线不含 007 的成果 → 008 正确 block 报障。
2. 阶段 7：T-ALERT-010（数据地图 + 路由器）与 T-ALERT-011（服务接口 + 前端）**有依赖**
   —— 011 要调 010 的 `alerts.router.ask()`、要读 010 的数据地图 JSON。
   我又并行派了 → 011 同样正确 block 报障（深蓝king 附了完整核验证据，
   明确拒绝用桩凑数、不假 DONE）。

两次都是**豆包正确、我错误**。所以要在派单**之前**检测。

检测口径
--------
任务的 allowed_paths 里若引用了**另一个任务**才会产出的文件 → 两者有依赖。
本脚本用一张显式的依赖表（比字符串猜更可靠），并提供：
  * check_dependencies(tasks) → 返回有依赖但被并行派发的任务对
  * order_tasks(tasks)      → 按依赖拓扑排序（先派上游）

用法（在派单脚本里）：
    tasks = [t1, t2]
    violations = check_dependencies(tasks)
    if violations:
        raise SystemExit("检测到有依赖的任务被并行派发：%s" % violations)
    for spec in order_tasks(tasks):
        dispatch(spec)
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

#: 依赖表：下游任务 → 它依赖的上游任务
#: （依据：下游的 allowed_paths 里引用了上游才产出的文件）
DEPENDENCIES: Dict[str, Tuple[str, ...]] = {
    "T-ALERT-008": ("T-ALERT-007",),   # 阶段5-B 依赖 阶段5-A 的 auto_release.py
    "T-ALERT-011": ("T-ALERT-010",),   # 阶段7-B 依赖 阶段7-A 的 router.py 与数据地图
}


def _ids(tasks: Sequence[dict]) -> List[str]:
    return [str(t.get("task_id") or "") for t in tasks]


def check_dependencies(tasks: Sequence[dict]) -> List[Tuple[str, str, str]]:
    """返回有依赖但**被并行派发**的任务对 (下游, 上游, 原因)。

    「并行派发」= 在同一批 tasks 里同时出现，且上游的产物尚未在下游的基线里。
    这里按**同批出现**判定（保守：宁可提醒也不要漏）。
    """
    ids = _ids(tasks)
    violations = []
    for down in ids:
        for up in DEPENDENCIES.get(down, ()):
            if up in ids:
                violations.append((down, up, "同批派发，但 %s 依赖 %s 的产物" % (down, up)))
    return violations


def order_tasks(tasks: Sequence[dict]) -> List[dict]:
    """按依赖拓扑排序：上游在前。"""
    by_id = {str(t.get("task_id")): t for t in tasks}
    ordered: List[dict] = []
    seen: set = set()

    def visit(tid: str, stack: tuple = ()) -> None:
        if tid in seen or tid not in by_id:
            return
        if tid in stack:
            raise ValueError("依赖成环：%s" % (" -> ".join(stack + (tid,))))
        for up in DEPENDENCIES.get(tid, ()):
            visit(up, stack + (tid,))
        seen.add(tid)
        ordered.append(by_id[tid])

    for tid in _ids(tasks):
        visit(tid)
    return ordered


def upstream_deliverables(task_id: str) -> List[str]:
    """某任务所依赖的上游交付物（供派工单里写明"前置由 X 提供"）。"""
    return list(DEPENDENCIES.get(task_id, ()))
