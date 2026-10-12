"""orchestrator.core.heal
阶段 6：失败自愈（verify 驱动的全局回灌循环）与失败交接。

循环：verify 未通过（任一 required 失败）→ 把失败明细回灌给同一 adapter
（"仅修复以下失败，不要扩大改动范围"）→ 重新 verify；轮次受 max_retries()
约束，每轮写 logs/heal-<n>.log（stdout/stderr 分开存）与 events.jsonl。
每轮重派发同样过 policy 闸门。

用尽 → 失败交接：保留 worktree（打印路径）、生成 specs/<task-id>/report.md
（做了什么/卡在哪/已试轮次/建议下一步）；可选 draft PR 由 push 阶段附带 blocked.md。
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from .state import TaskState, log_event, redact, save as save_state


def max_retries(config: dict[str, Any]) -> int:
    """heal 回灌轮次上限：优先顶层 max_retry，兼容旧 runtime.max_heal_retries。"""
    if "max_retry" in config:
        return int(config["max_retry"])
    return int(config.get("runtime", {}).get("max_heal_retries", 3))


def retry_hint(attempt: int, exit_code: int, stderr_tail: str = "",
               stdout_tail: str = "") -> str:
    """逐段/整份 heal 的回灌文案：只针对本轮失败，严禁扩大范围。"""
    parts = [
        f"【自愈第 {attempt} 轮】上一轮执行失败（exit_code={exit_code}）。",
        "仅修复以下失败，不要扩大改动范围，不要重写已通过部分：",
    ]
    if stderr_tail.strip():
        parts.append("--- stderr (tail) ---\n" + stderr_tail.strip()[-1500:])
    if stdout_tail.strip():
        parts.append("--- stdout (tail) ---\n" + stdout_tail.strip()[-800:])
    return "\n".join(parts)


def failure_block(report: dict[str, Any]) -> str:
    """把 verify_report 的 required 失败渲染成回灌文本。"""
    lines = ["验收未通过，以下 required 命令失败："]
    for r in report.get("results", []):
        if r.get("required") and not r.get("passed"):
            lines.append(
                f"- 命令: {r['command']}\n"
                f"  exit={r.get('returncode')} timed_out={r.get('timed_out')}\n"
                f"  stderr(tail): {(r.get('stderr') or '').strip()[-500:] or '(空)'}"
            )
    return "\n".join(lines)


def make_report(task_dir: Path, *, stage: str, did: list[str], stuck: str,
                tried: list[str], next_steps: list[str], worktree: str,
                extra: str = "") -> Path:
    """失败交接文档 specs/<task-id>/report.md：做了什么/卡在哪/已试轮次/建议下一步。"""
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    lines = [
        f"# 失败交接 report.md（{stage}）",
        "",
        f"- 时间: {stamp}",
        f"- 隔离工作区（已保留）: {worktree}",
        "",
        "## 做了什么",
    ] + [f"- {d}" for d in did] or ["- （无）"]
    lines += ["", "## 卡在哪", f"- {stuck}", "", "## 已试轮次"]
    lines += [f"- {t}" for t in tried] if tried else ["- （无）"]
    lines += ["", "## 建议下一步"]
    lines += [f"- {s}" for s in next_steps]
    if extra:
        lines += ["", extra]
    path = Path(task_dir) / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(
    task_dir: Path,
    adapter: Any,
    config: dict[str, Any],
    worktree: Path,
    policy_mode: str = "auto",
    state: TaskState | None = None,
) -> dict[str, Any]:
    """verify→回灌→再 verify 的自愈循环（首验也由本函数执行）。

    返回 status: verified | heal_exhausted | forbidden | rejected | ci_fail | blocked
    ok=True 仅 verified。失败交接时 worktree 保留并生成 report.md。
    """
    from . import dispatch as _dispatch   # 延迟导入破环
    from . import verify as _verify

    task_dir = Path(task_dir)
    max_retry = max_retries(config)
    prompt_md = (task_dir / "prompt.md").read_text(encoding="utf-8")

    report = _verify.run(task_dir, config, worktree=worktree)
    log_event(task_dir, "verify", "round",
              passed=report.get("passed"), failed=report.get("failed"))
    attempt = 0
    while not report.get("passed"):
        if attempt >= max_retry:
            tried = [f"第 {i + 1} 轮自愈后仍失败" for i in range(attempt)]
            make_report(
                task_dir, stage="verify/heal",
                did=["explore→compile→dispatch 已完成；acceptance 命令逐条执行过"],
                stuck=f"{report.get('required_failed')} 仍失败（heal 用尽 {max_retry} 轮）",
                tried=tried,
                next_steps=[
                    f"进入隔离区人工修复: {worktree}",
                    "修复后 `orch run <task-id> --resume` 从 verify 继续",
                    "或完善 acceptance.md/prompt.md 后重跑",
                ],
                worktree=str(worktree),
                extra="> 可选：`orch push --task <id> --open-pr` 开 draft PR 附 blocked.md @ 评审人。",
            )
            log_event(task_dir, "heal", "exhausted", attempts=attempt)
            return {"ok": False, "status": "heal_exhausted", "attempts": attempt,
                    "report": report, "worktree": str(worktree)}

        attempt += 1
        hint = (f"\n\n【验收自愈第 {attempt} 轮】\n"
                + failure_block(report)
                + "\n仅修复以下失败，不要扩大改动范围，不要重写已通过部分。")
        result = _dispatch.execute_and_log(
            task_dir, adapter, prompt_md + hint, worktree, config,
            log_tag=f"heal{attempt}")
        outcome = _dispatch.judge_writes(task_dir, result, config, mode=policy_mode)
        # 每轮独立 logs/heal-<n>.log：stdout/stderr 分开存（脱敏）
        (task_dir / "logs" / f"heal-{attempt}.log").write_text(
            f"exit_code={result.exit_code}\n--- stdout ---\n{redact(result.stdout)}"
            f"\n--- stderr ---\n{redact(result.stderr)}\n", encoding="utf-8")
        log_event(task_dir, "heal", "round", attempt=attempt,
                  exit_code=result.exit_code, verdict=outcome.status)
        if state is not None:
            state.heal_attempts = attempt
            save_state(task_dir, state)
        if not outcome.ok:
            return {"ok": False, "status": outcome.status, "attempts": attempt,
                    "report": report, "message": outcome.message}
        report = _verify.run(task_dir, config, worktree=worktree)

    return {"ok": True, "status": "verified", "attempts": attempt, "report": report}
