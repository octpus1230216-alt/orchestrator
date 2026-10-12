"""orchestrator.core.state
流水线状态机与断点续跑（state.json 是核心数据契约，文件即接口）。

state.json 约定结构：
{
  "task_id": "...",
  "stage": "explore|confirm|compile|dispatch|verify|push",
  "stages_done": [...],
  "segment_index": 2,            // 分段进度（prompt.md `## SEGMENT n`）
  "segments": [...],             // 段索引/摘要
  "worktree": "path",            // 隔离工作区
  "skills_used": [...],
  "budget": {"llm_calls": 5, "tokens": 12000, "usd": 0.8},
  "fingerprint_mode": "file_bytes",
  "doc_id": "sha256:..."         // 本任务规格文档指纹
}

职责：
- 每阶段完成即写 state.json（checkpoint）
- resume：返回第一个未完成阶段，从最后成功阶段继续，不重跑已完成阶段
- 预算上限：max_llm_calls / max_tokens / max_usd，任一超限即暂停
- 成本累计供 logs/cost.json 落盘
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

#: 主流水线阶段顺序（heal 为 verify 失败后的重试循环，不计为独立阶段）
STAGES = ["explore", "confirm", "compile", "dispatch", "verify", "push"]


@dataclass
class Budget:
    """已累计成本（对应 state.json.budget）。"""

    llm_calls: int = 0
    tokens: int = 0
    usd: float = 0.0


@dataclass
class BudgetLimits:
    """预算上限（来自 config.runtime）。"""

    max_llm_calls: int | None = None
    max_tokens: int | None = None
    max_usd: float | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "BudgetLimits":
        rt = config.get("runtime", {})
        return cls(
            max_llm_calls=rt.get("max_llm_calls"),
            max_tokens=rt.get("max_tokens"),
            # 兼容旧字段名 max_budget_usd
            max_usd=rt.get("max_usd", rt.get("max_budget_usd")),
        )


@dataclass
class TaskState:
    """specs/<task-id>/state.json 的内存表示（严格对齐契约字段名）。"""

    task_id: str
    stage: str = "explore"
    stages_done: list[str] = field(default_factory=list)
    segment_index: int = 0
    segments: list[Any] = field(default_factory=list)
    worktree: str = ""
    skills_used: list[str] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    fingerprint_mode: str = "file_bytes"
    doc_id: str = ""

    # ---- 契约之外的运行辅助（同样落盘，便于续跑/自愈）------------
    agent: str = ""
    heal_attempts: int = 0
    updated_at: float = field(default_factory=time.time)
    extras: dict[str, Any] = field(default_factory=dict)

    # ---- 阶段游标 ------------------------------------------------
    def stage_index(self) -> int:
        return STAGES.index(self.stage) if self.stage in STAGES else -1

    def next_stage(self) -> str | None:
        """resume 入口：返回第一个尚未完成的阶段；全部完成返回 None。"""
        for s in STAGES:
            if s not in self.stages_done:
                return s
        return None

    def is_finished(self) -> bool:
        return all(s in self.stages_done for s in STAGES)


# ---- 构造 / 持久化 ------------------------------------------------
def new_state(task_id: str, agent: str = "", config: dict[str, Any] | None = None) -> TaskState:
    """为全新任务初始化 state（stage 从 explore 起）。"""
    return TaskState(task_id=task_id, agent=agent)


def save(task_dir: Path, state: TaskState) -> Path:
    """每阶段完成即写 specs/<task-id>/state.json（checkpoint）。"""
    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    state.updated_at = time.time()
    path = task_dir / "state.json"
    path.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load(task_dir: Path) -> TaskState:
    """从 state.json 恢复状态（断点续跑入口）。"""
    path = Path(task_dir) / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return from_dict(data)


def from_dict(data: dict[str, Any]) -> TaskState:
    """把 state.json 字典还原为 TaskState（重建嵌套 Budget）。"""
    data = dict(data)
    budget = data.pop("budget", {}) or {}
    state = TaskState(**data)
    state.budget = Budget(**{k: v for k, v in budget.items() if k in Budget.__dataclass_fields__})
    return state


# ---- 阶段推进 -----------------------------------------------------
def mark_done(state: TaskState, stage: str) -> TaskState:
    """标记某阶段完成，并把 stage 游标推进到下一个未完成阶段。"""
    if stage not in state.stages_done:
        state.stages_done.append(stage)
    state.stage = state.next_stage() or stage
    return state


# ---- 分段进度（P5：prompt.md `## SEGMENT n`）----------------------
def set_segments(state: TaskState, segments: list[Any]) -> TaskState:
    state.segments = list(segments)
    state.segment_index = 0
    return state


def advance_segment(state: TaskState) -> TaskState:
    """完成当前段后前进一段，越界则夹到末段。"""
    state.segment_index = min(state.segment_index + 1, max(len(state.segments) - 1, 0))
    return state


# ---- 成本 / 预算 --------------------------------------------------
def add_cost(
    state: TaskState,
    *,
    llm_calls: int = 0,
    tokens: int = 0,
    usd: float = 0.0,
) -> TaskState:
    """累计一次操作的成本。"""
    state.budget.llm_calls += llm_calls
    state.budget.tokens += tokens
    state.budget.usd = round(state.budget.usd + usd, 6)
    return state


def over_budget(state: TaskState, limits: BudgetLimits) -> str | None:
    """返回首个被突破的预算维度名，未超限返回 None。"""
    b = state.budget
    if limits.max_llm_calls is not None and b.llm_calls >= limits.max_llm_calls:
        return "max_llm_calls"
    if limits.max_tokens is not None and b.tokens >= limits.max_tokens:
        return "max_tokens"
    if limits.max_usd is not None and b.usd >= limits.max_usd:
        return "max_usd"
    return None


def budget_progress(state: TaskState, limits: BudgetLimits) -> str:
    """供暂停时打印的进度摘要。"""
    b = state.budget
    return (
        f"stage={state.stage} segments={state.segment_index}/{len(state.segments)} "
        f"budget[llm_calls={b.llm_calls}/{limits.max_llm_calls} "
        f"tokens={b.tokens}/{limits.max_tokens} usd={b.usd}/{limits.max_usd}] "
        f"heal={state.heal_attempts}"
    )


def record_cost_log(task_dir: Path, state: TaskState, note: str = "") -> Path:
    """把当前成本写入 logs/cost.json（覆盖式快照）。"""
    log_dir = Path(task_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "cost.json"
    payload = {
        "task_id": state.task_id,
        "stage": state.stage,
        "updated_at": state.updated_at,
        "budget": asdict(state.budget),
        "heal_attempts": state.heal_attempts,
        "note": note,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---- 可观测性：结构化事件日志 + 脱敏 --------------------------------
#: api_key/token/secret/authorization/bearer 的值一律打码（密钥不进 log）
_SECRET_RE = re.compile(
    r"(api[_-]?key|token|secret|authorization|bearer)([\"'=: ]+)([^\s\"',;]+)",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """脱敏文本中的密钥/token 取值；所有落盘日志前都应过一遍。"""
    if not isinstance(text, str):
        text = str(text)
    return _SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "***", text)


def log_event(task_dir: Path, stage: str, event: str, **fields: Any) -> Path:
    """追写一行结构化日志 logs/events.jsonl（JSON lines）。

    字符串字段统一过 redact，密钥不落地。
    """
    log_dir = Path(task_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "events.jsonl"
    row = {"ts": datetime.now().isoformat(timespec="seconds"),
           "stage": stage, "event": event}
    for k, v in fields.items():
        row[k] = redact(v) if isinstance(v, str) else v
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path
