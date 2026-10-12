"""orchestrator.core.approval
审批交互：承接 policy 判定为 REQUIRE / FORBID 的写操作。

模式矩阵（mode: interactive | auto | yolo，默认 auto）：
| 操作       | interactive | auto            | yolo      |
| L1/L2      | 放行        | 放行            | 放行      |
| L3         | 询问        | 询问（CI fail） | 放行+警告 |
| forbidden  | 拒绝        | 拒绝            | 拒绝      |

- REQUIRE 暂停并逐条交互：[a]pprove [r]eject [e]dit [A]pprove all in this category
- 无 TTY（CI）：auto → fail 并输出待批清单；interactive → 直接 fail（不 hang）
- FORBID → 拒绝 + 回灌替代建议（写 blocked.md）
- 所有决策记 approvals.log（时间/操作/目标/原因/决策/模式）
- L2（record_levels）自动放行但记 logs/change-summary.md
"""
from __future__ import annotations

import datetime as _dt
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .policy import AUTO, FORBID, REQUIRE, PolicyResult

# forbidden → 回灌给 agent 的替代建议
_ALTERNATIVES = {
    "force_push": "不要 force push：在隔离分支 feat/<task-id> 上追加普通 commit，"
                  "再走 PR 合并；确需改写历史请人工操作并另行说明。",
    "push_main_directly": "不要直推 main：推送到隔离分支后走 `orch push --open-pr` 创建 PR。",
}


@dataclass
class ApprovalOutcome:
    """一次审批处理的结果。ok=False 时 message 可直接回灌/展示。"""

    ok: bool
    status: str                     # auto | approved | rejected | forbidden | ci_fail | yolo_pass
    message: str = ""
    pending: list[Any] = field(default_factory=list)   # 待批清单（ci_fail 时输出）


def has_tty(is_tty: Optional[bool] = None) -> bool:
    """检测交互终端；测试可用 is_tty 覆盖。"""
    if is_tty is not None:
        return is_tty
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        return False


def _log(task_dir: Path, decision: str, trigger: str, target: str,
         reason: str, mode: str) -> None:
    """approvals.log：时间/操作/目标/原因/决策/模式。"""
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    line = (f"{stamp} | {decision:<9} | mode={mode} | op={trigger} "
            f"| target={target} | reason={reason or '-'}\n")
    with (task_dir / "approvals.log").open("a", encoding="utf-8") as fh:
        fh.write(line)


def _record_l2(task_dir: Path, result: PolicyResult, config: dict[str, Any]) -> None:
    """L2 改动自动放行但记 logs/change-summary.md（record_levels 控制）。"""
    record = (config or {}).get("policy", {}).get("record_levels", ["L2"])
    items = [j for j in result.judgements if j.level in record and j.verdict == AUTO]
    if not items:
        return
    logs = task_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    with (logs / "change-summary.md").open("a", encoding="utf-8") as fh:
        if fh.tell() == 0:
            fh.write("# L2 改动摘要（自动放行，留档）\n")
        for j in items:
            w = j.write
            fh.write(f"- {stamp} [{j.level}] `{w.op}` {w.path or w.symbol}"
                     f"{' :: ' + w.symbol if w.symbol and w.path else ''}"
                     f"（{w.reason or j.reason}）\n")


def _render(judgement: Any) -> str:
    w = judgement.write
    target = f"{w.path} :: {w.symbol}" if w.path and w.symbol else (w.path or w.symbol or w.op)
    return (
        f"[审批] agent 请求：{judgement.trigger or w.op}\n"
        f"   目标：{target}\n"
        f"   原因：{w.reason or '（agent 未给出）'}\n"
        f"   [a]pprove [r]eject [e]dit [A]pprove all in this category"
    )


def _ask(judgement: Any, input_func: Callable[[str], str]) -> str:
    """单条询问，返回 approve | reject | approve_all | edit（区分大小写：a vs A）。"""
    print(_render(judgement))
    try:
        choice = input_func("> ").strip()
    except EOFError:  # 无标准输入（被 isatty 误报的罕见环境）：不 hang，按拒绝
        print("  无标准输入（EOF），按 reject 处理（不 hang）。")
        return "reject"
    if choice == "a":
        return "approve"
    if choice == "A":
        return "approve_all"
    if choice == "r":
        return "reject"
    if choice == "e":
        return "edit"
    print(f"  未识别输入 {choice!r}，按 reject 处理（不 hang）。")
    return "reject"


def handle(task_dir: Path, result: PolicyResult, mode: str = "auto",
           config: dict[str, Any] | None = None,
           input_func: Callable[[str], str] = input,
           is_tty: Optional[bool] = None) -> ApprovalOutcome:
    """按模式矩阵处理一次聚合判定。L1/L2 直接过；REQUIRE 询问；FORBID 拒绝。"""
    task_dir = Path(task_dir)
    config = config or {}

    # 优先级 1：forbidden → 任何模式都拒绝 + 回灌替代建议
    if result.forbidden:
        advice: list[str] = []
        lines = ["# 已拒绝（forbidden），待人工处理\n"]
        for j in result.forbidden:
            w = j.write
            alt = _ALTERNATIVES.get(j.trigger, "停止该操作，改为在隔离分支上做低风险替代并说明原因。")
            advice.append(alt)
            _log(task_dir, "FORBIDDEN", j.trigger or w.op, w.target, w.reason, mode)
            lines.append(f"- 操作: {w.op}  目标: {w.target}  原因: {w.reason or '-'}")
            lines.append(f"  替代建议: {alt}")
        (task_dir / "blocked.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ApprovalOutcome(
            ok=False, status="forbidden",
            message="forbidden 已拒绝。回灌替代建议：\n- " + "\n- ".join(advice),
        )

    # L2 留档（无论后续 REQUIRE 结果如何，AUTO 部分已发生）
    _record_l2(task_dir, result, config)

    pending = result.pending
    if not pending:
        return ApprovalOutcome(ok=True, status="auto", message="无需审批，自动放行")

    # yolo：L3 放行 + 警告（仍留审计日志）
    if mode == "yolo":
        for j in pending:
            _log(task_dir, "YOLO_PASS", j.trigger or j.write.op, j.write.target, j.write.reason, mode)
            print(f"[审批] [警告] yolo 模式放行 L3：{j.trigger} -> {j.write.target}")
        return ApprovalOutcome(ok=True, status="yolo_pass",
                               message=f"yolo：{len(pending)} 条 L3 已放行（有警告）")

    # interactive / auto：L3 询问
    if not has_tty(is_tty):
        # 无 TTY：auto → fail 并输出待批清单；interactive → 直接 fail（不 hang）
        listing = []
        for j in pending:
            _log(task_dir, "PENDING", j.trigger or j.write.op, j.write.target, j.write.reason, mode)
            listing.append(f"  - [{j.trigger}] {j.write.target}（{j.write.reason or '-'}）")
        msg = ("无终端可交互（CI），L3 审批无法完成 → fail。待批清单：\n"
               + "\n".join(listing)
               + f"\n人工处理：`orch approve --task {task_dir.name}`")
        return ApprovalOutcome(ok=False, status="ci_fail", message=msg, pending=pending)

    approved_all: set[str] = set()   # 'A' 记住的整类触发项
    for j in pending:
        trigger = j.trigger or j.write.op
        if trigger in approved_all:
            _log(task_dir, "APPROVED", trigger, j.write.target, j.write.reason, mode)
            continue
        while True:
            decision = _ask(j, input_func)
            if decision == "approve":
                _log(task_dir, "APPROVED", trigger, j.write.target, j.write.reason, mode)
                break
            if decision == "approve_all":
                approved_all.add(trigger)
                _log(task_dir, "APPROVED_ALL", trigger, j.write.target, j.write.reason, mode)
                break
            if decision == "edit":
                new_reason = input_func("  输入修订后的原因/范围（回车跳过）: ").strip()
                if new_reason:
                    j.write.reason = new_reason
                    _log(task_dir, "EDITED", trigger, j.write.target, new_reason, mode)
                    continue          # 修订后重新询问
                break                  # 跳过编辑视为放弃本轮 → reject
            # reject
            _log(task_dir, "REJECTED", trigger, j.write.target, j.write.reason, mode)
            return ApprovalOutcome(
                ok=False, status="rejected",
                message=f"已拒绝 {trigger} -> {j.write.target}；请将该结论回灌 agent 修正。",
                pending=pending,
            )

    return ApprovalOutcome(ok=True, status="approved",
                           message=f"{len(pending)} 条 L3 审批完成")
