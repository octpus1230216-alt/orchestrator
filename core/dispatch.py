"""orchestrator.core.dispatch
阶段 4：派发执行（支持整份喂入与分段喂入）。

流程：读 prompt.md → 解析 `## SEGMENT n` → 按 segment_mode 选择：
  whole        一次性喂整份 prompt.md（run）
  per_segment  逐段自动喂入（run_segments）：段间只注入 context 全文 + 前序段
               文件清单 + 一行摘要，不重复塞整份前序 prompt；失败 → 逐段 heal；
               每段完成写 state.json，--resume 从最后未成功段继续。
两种模式均：adapter.run(policy 须知注入) → 收集写操作 → policy 三档判定 → approval 模式矩阵。

新增一家 agent 只加一个 adapter 薄文件 + config.yaml adapters 一条，本模块无需改动。
"""
from __future__ import annotations

import datetime as _dt
import importlib
from pathlib import Path
from typing import Any

from adapters.base import AgentAdapter
from . import approval, heal, policy
from .compile import parse_segments
from .state import TaskState, log_event, redact, save as save_state

_POLICY_HINT = (
    "【权限闸门须知】你在隔离 git worktree 中执行；所有写操作将被编排器审计并三档判定"
    "（默认 auto，只拦 L3）。禁止 force push、禁止直推 main（命中即拒绝）；"
    "改公共签名/范围/schema/依赖/CI/config/Dockerfile/lock、删文件、改安全鉴权支付等 L3 操作"
    "会被要求人工审批。保持改动最小、不扩大范围，拿不准时写 blocked.md 说明，不要擅自决定。"
)

# name-status 状态字符 → WriteOp 类型（diff 推断路径）
_STATUS_OP = {
    "D": "delete_file",       # L3
    "A": "create_file",       # L2
    "?": "create_file",       # L2（未跟踪新文件）
    "R": "rename_file",       # L2（内部重构）
    "C": "rename_file",
    "M": "modify_file",       # L1（除非路径升级）
}


def load_adapter(agent_name: str, config: dict[str, Any]) -> AgentAdapter:
    """按 config.adapters.<name> 动态加载薄层 adapter 并注入模板/能力配置。"""
    entries = config.get("adapters", {})
    if agent_name not in entries:
        raise KeyError(
            f"config.yaml adapters 未注册 agent: {agent_name}（可用: {sorted(entries)}）"
        )
    module = importlib.import_module(f"adapters.{agent_name}")
    return module.build(entries[agent_name])


def _is_internal(path: str) -> bool:
    """编排器内部产物（.orch/ 下的 prompt 文件等）不属于 agent 改动，不计入审计（R2）。"""
    p = (path or "").replace("\\", "/")
    return p.startswith(".orch/") or "/.orch/" in p


def collect_writes(result: Any) -> list[policy.WriteOp]:
    """把 adapter 产出的写操作统一成 WriteOp 列表。

    - supports_write_requests 的 agent：直接用声明的 op/path/reason/symbol；
    - 其余：由 git name-status（'M\\tpath'）推断类型；路径升级规则在 policy 里生效。
    - 两侧均过滤 .orch/ 内部路径（审计 R2）。
    """
    writes: list[policy.WriteOp] = []
    for req in getattr(result, "write_requests", []) or []:
        path = str(req.get("path", ""))
        if _is_internal(path):
            continue
        writes.append(policy.WriteOp(
            op=str(req.get("op", "modify_file")),
            path=path,
            reason=str(req.get("reason", "")),
            symbol=str(req.get("symbol", "")),
            raw=str(req),
        ))
    for line in getattr(result, "files_changed", []) or []:
        parts = line.split("\t")
        if len(parts) >= 2:
            status, path = parts[0].strip(), parts[-1]
        else:
            status, path = "M", parts[0]
        if _is_internal(path):
            continue
        writes.append(policy.WriteOp(
            op=_STATUS_OP.get(status[0] if status else "M", "modify_file"),
            path=path, raw=line,
        ))
    return writes


def judge_writes(task_dir: Path, result: Any, config: dict[str, Any],
                 mode: str = "auto") -> approval.ApprovalOutcome:
    """收集到的写操作交 policy 三档判定，再按模式矩阵走 approval。"""
    writes = collect_writes(result)
    pres = policy.decide(writes, config)
    return approval.handle(Path(task_dir), pres, mode=mode, config=config)


def execute_and_log(task_dir: Path, adapter: AgentAdapter, payload: str,
                    worktree: Path, config: dict[str, Any],
                    log_tag: str = "") -> Any:
    """单次派发 + 执行日志落 logs/（stdout/stderr 分开存，均过脱敏）。"""
    timeout = config.get("timeouts", {}).get("agent", 1800)
    result = adapter.run(payload, worktree, timeout=timeout, policy_hint=_POLICY_HINT)
    logs = task_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = f"-{log_tag}" if log_tag else ""
    (logs / f"dispatch-{stamp}{tag}.log").write_text(
        f"exit_code={result.exit_code}\n--- stdout ---\n{redact(result.stdout)}\n"
        f"--- stderr ---\n{redact(result.stderr)}\n",
        encoding="utf-8",
    )
    return result


def run(
    task_dir: Path,
    adapter: AgentAdapter,
    config: dict[str, Any],
    worktree: Path,
    mode: str = "auto",
) -> dict[str, Any]:
    """whole 模式：在隔离 worktree 一次性派发整份 prompt.md，返回派发摘要。"""
    task_dir = Path(task_dir)
    prompt = (task_dir / "prompt.md").read_text(encoding="utf-8")

    result = execute_and_log(task_dir, adapter, prompt, worktree, config)
    outcome = judge_writes(task_dir, result, config, mode=mode)
    log_event(task_dir, "dispatch", "whole",
              exit_code=result.exit_code, verdict=outcome.status,
              changes=len(result.files_changed))
    return {
        "exit_code": result.exit_code,
        "ok": result.exit_code == 0 and outcome.ok,
        "files_changed": result.files_changed,
        "write_requests": result.write_requests,
        "status": outcome.status,          # auto|approved|rejected|forbidden|ci_fail|yolo_pass
        "segment_mode": "whole",
        "blocked": not outcome.ok,
        "message": outcome.message,
    }


# ===================== 分段喂入（P5：segment dispatch） =====================

SEG_WHOLE = "whole"
SEG_PER = "per_segment"

#: 分段判定默认阈值（审计 R5：全链唯一判定函数 + 唯一 config 键 segment_threshold，
#: 本常量是唯一默认值来源；compile 侧曾有第二套阈值的死引用已删除）
DEFAULT_SEGMENT_THRESHOLD = 6000


def resolve_segment_mode(prompt_len: int, config: dict[str, Any],
                         override: str | None = None) -> str:
    """whole | per_segment | auto（字符数 < threshold → whole，否则 per_segment）。

    override 来自命令行 --segment，优先于 config.segment_mode（cli 只覆盖不取代）。
    阈值只读顶层 config.segment_threshold（单源）。
    """
    seg_mode = (override or config.get("segment_mode") or "auto").strip().lower()
    if seg_mode in (SEG_WHOLE, SEG_PER):
        return seg_mode
    threshold = config.get("segment_threshold")
    if threshold is None:
        threshold = DEFAULT_SEGMENT_THRESHOLD
    return SEG_WHOLE if prompt_len < int(threshold) else SEG_PER


def build_segment_prompt(seg_body: str, context_md: str = "",
                         prev_files: list[str] | None = None,
                         prev_summaries: list[str] | None = None) -> str:
    """拼装单段输入：本段正文 + 段间上下文（防 token 爆炸）。

    后续段只带：context.md 全文 + 前序段文件清单 + 前序段一行摘要；
    绝不重复塞前序段的整份 prompt。
    """
    parts = [seg_body]
    if prev_files or prev_summaries or context_md:
        block = ["## 已完成进度（编排器注入，勿重跑前序段）"]
        if prev_summaries:
            block.append("### 前序段一行摘要\n" + "\n".join(f"- {s}" for s in prev_summaries))
        if prev_files:
            block.append("### 前序段已产出文件\n" + "\n".join(f"- {f}" for f in prev_files))
        if context_md:
            block.append("### 任务上下文（context.md 全文）\n" + context_md)
        block.append("> 请直接基于上述文件继续本段任务；不要重新阅读或重实现前序段内容。")
        parts.append("\n\n".join(block))
    return "\n\n".join(parts) + "\n"


def _seg_summary_line(seg: dict[str, Any]) -> str:
    """段的一行摘要：正文首行非标题文本，截 80 字。"""
    for line in (seg.get("body") or "").splitlines():
        text = line.strip()
        if text and not text.startswith(("#", "-", ">", "`")):
            return text[:80]
    return (seg.get("title") or f"SEGMENT {seg.get('index')}")[:80]


def _paths(entries: list[str]) -> list[str]:
    return [e.split("\t")[-1] for e in entries if e.strip()]


def _handoff_failure(task_dir: Path, seg: dict[str, Any], result: Any,
                     max_retry: int, state: TaskState | None,
                     done_info: list[dict[str, Any]], worktree: Path) -> dict[str, Any]:
    """某段 heal 用尽 → 失败交接（P6）：保留已完成段产物，state 标记失败段。"""
    lines = [
        "# 失败交接（segment heal 用尽）\n",
        f"- 失败段：SEGMENT {seg['index']}（已重试 {max_retry} 轮）",
        f"- 最后 exit_code：{result.exit_code}",
        "",
        "## 最后一段 stderr 摘录",
        "```\n" + (result.stderr or result.stdout or "(无输出)").strip()[-1200:] + "\n```",
        "",
        "## 已完成段产物（已保留在工作区）",
    ]
    if done_info:
        for d in done_info:
            lines.append(f"- SEGMENT {d['index']}: {d['summary']}")
            lines += [f"    {p}" for p in d["files"]]
    else:
        lines.append("- （无，首段即失败）")
    lines += ["", "> 人工处理后可 `orch run <task-id> --resume` 从失败段继续。"]
    (task_dir / "blocked.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 失败交接 report.md（做了什么/卡在哪/已试轮次/建议下一步）；worktree 保留
    heal.make_report(
        task_dir, stage=f"dispatch/segment {seg['index']}",
        did=[f"SEGMENT {d['index']} 完成: {d['summary']}" for d in done_info]
            or ["无段完成（首段即失败）"],
        stuck=f"SEGMENT {seg['index']} 反复失败（exit={result.exit_code}），heal 已试 {max_retry} 轮",
        tried=[f"首次执行 + 第 {i + 1} 轮回灌重试，均 exit!=0" for i in range(max_retry)],
        next_steps=[
            f"进入隔离区人工修复: {worktree}",
            f"修复后 `orch run <task-id> --resume` 从 SEGMENT {seg['index']} 继续（已完成段不重跑）",
        ],
        worktree=str(worktree),
    )
    log_event(task_dir, "dispatch", "segment_failed", segment=seg["index"],
              attempts=max_retry, exit_code=result.exit_code)
    if state is not None:
        state.extras["failed_segment"] = seg["index"]
        save_state(task_dir, state)
    return {
        "exit_code": result.exit_code,
        "ok": False,
        "status": "segment_failed",
        "failed_segment": seg["index"],
        "segment_mode": SEG_PER,
        "blocked": True,
        "files_changed": [f for d in done_info for f in d["raw"]],
        "message": f"SEGMENT {seg['index']} heal 用尽（{max_retry} 轮）→ 失败交接，见 report.md/blocked.md",
    }


def run_segments(
    task_dir: Path,
    adapter: AgentAdapter,
    config: dict[str, Any],
    worktree: Path,
    mode: str = "auto",
    state: TaskState | None = None,
) -> dict[str, Any]:
    """per_segment 模式：逐段自动喂入，全程无需人工（除非触发 L3）。

    断点续跑：state.segment_index = 已完成段数；resume 从最后未成功段继续，
    不重跑已完成段；每段完成即 checkpoint 写 state.json。
    """
    task_dir = Path(task_dir)
    prompt_md = (task_dir / "prompt.md").read_text(encoding="utf-8")
    segments = parse_segments(prompt_md)
    context_md = ""
    ctx_file = task_dir / "context.md"
    if ctx_file.exists():
        context_md = ctx_file.read_text(encoding="utf-8")

    max_retry = heal.max_retries(config)
    start = max(int(getattr(state, "segment_index", 0) or 0), 0)
    done_info: list[dict[str, Any]] = list(
        (getattr(state, "extras", {}) or {}).get("segments_done", []) if state else []
    )
    # 已完成段的历史（resume 时从 state.extras 恢复）
    prev_files = [p for d in done_info for p in d.get("files", [])]
    prev_summaries = [f"SEGMENT {d['index']}: {d['summary']}" for d in done_info]

    for idx in range(start, len(segments)):
        seg = segments[idx]
        base_body = seg["body"]
        payload = (
            build_segment_prompt(base_body, context_md, prev_files, prev_summaries)
            if idx > 0 else base_body + "\n"
        )

        attempt = 0
        while True:
            result = execute_and_log(task_dir, adapter, payload, worktree, config,
                                     log_tag=f"seg{seg['index']}" + (f"-try{attempt}" if attempt else ""))
            outcome = judge_writes(task_dir, result, config, mode=mode)
            if not outcome.ok:
                # L3/forbidden/ci_fail：停下交人工，不推进段游标
                return {
                    "exit_code": result.exit_code,
                    "ok": False,
                    "status": outcome.status,
                    "failed_segment": seg["index"],
                    "segment_mode": SEG_PER,
                    "blocked": True,
                    # 上报用真实 name-status 行（A/M/D/R…），不能一律标 M：
                    # 否则前序段的删除(D)会被误判 modify_file 而漏掉 delete_file L3 审批（R9）
                    "files_changed": [r for d in done_info for r in d["raw"]],
                    "write_requests": list(getattr(result, "write_requests", []) or []),
                    "message": outcome.message,
                }
            if result.exit_code == 0:
                break
            attempt += 1
            if attempt > max_retry:
                return _handoff_failure(task_dir, seg, result, max_retry, state,
                                        done_info, worktree)
            # 逐段 heal：只重喂本段 + 失败回灌，不携带前序段 prompt
            payload = build_segment_prompt(
                base_body + "\n\n" + heal.retry_hint(
                    attempt, result.exit_code, result.stderr, result.stdout),
                context_md, prev_files, prev_summaries,
            )

        # 段成功：记录产出，写段游标 checkpoint，下一段
        seg_info = {
            "index": seg["index"],
            "summary": _seg_summary_line(seg),
            "files": _paths(result.files_changed),
            "raw": list(result.files_changed),
        }
        done_info.append(seg_info)
        prev_files += seg_info["files"]
        prev_summaries.append(f"SEGMENT {seg['index']}: {seg_info['summary']}")
        log_event(task_dir, "dispatch", "segment_done", segment=seg["index"],
                  changes=seg_info["files"])
        if state is not None:
            state.segment_index = idx + 1
            state.extras["segments_done"] = done_info
            state.extras.pop("failed_segment", None)
            save_state(task_dir, state)

    return {
        "exit_code": 0,
        "ok": True,
        "status": "segments_done",
        "segment_mode": SEG_PER,
        "blocked": False,
        "files_changed": [r for d in done_info for r in d["raw"]],
        "segments_done": [d["index"] for d in done_info],
        "message": f"全部 {len(segments)} 段执行完成（其中本轮新跑 {len(segments) - start} 段）",
    }
