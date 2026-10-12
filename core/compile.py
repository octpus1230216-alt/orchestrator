"""orchestrator.core.compile
阶段 3：生成提示词。

读取任务目录已有的输入（不产出它们，审计 R11 与实现对齐）：
    context.md      —— explore 产出
    answers.json    —— confirm 产出
    skills.md / 选用技能文本 —— _template 播种与 core.skills
    acceptance.md   —— _template 播种、人工维护的任务**输入**（compile 不写它）
唯一产出：
    prompt.md       —— 交给 coding agent 的最终提示词（长内容按 `## SEGMENT n` 分段，
                       由 dispatch 逐段派发）
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from .llm import LLMClient

#: prompt.md 分段标题，如 `## SEGMENT 1`（长提示词按此拆分，P5 逐段派发）
_SEGMENT_RE = re.compile(r"^##\s+SEGMENT\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE)

_SYSTEM = (
    "你是任务编排助手。给定上下文、用户确认答案与全局技能/约束，"
    "合成一份交给 coding agent 的提示词（Markdown）。"
    "若内容较长，按逻辑边界分段，每段以 `## SEGMENT n` 标题开头（n 从 1 起）；"
    "段按序执行，段间共享上下文与已产出文件。只输出 Markdown 正文。"
)


def _read(task_dir: Path, name: str) -> str:
    f = Path(task_dir) / name
    return f.read_text(encoding="utf-8") if f.exists() else ""


def parse_segments(prompt_md: str) -> list[dict[str, Any]]:
    """把 prompt.md 按 `## SEGMENT n` 拆成段索引列表。

    返回 [{"index": int, "title": str, "body": str}, ...]；
    无分段标记时返回单一段（整篇作为一段）。
    """
    marks = list(_SEGMENT_RE.finditer(prompt_md))
    if not marks:
        return [{"index": 1, "title": "(whole)", "body": prompt_md.strip()}]

    segments: list[dict[str, Any]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(prompt_md)
        body = prompt_md[m.end():end].strip()
        segments.append({"index": int(m.group(1)), "title": m.group(0).strip(), "body": body})
    return segments


def run(task_dir: Path, config: dict[str, Any], skills_text: str = "",
        on_cost: Callable[[dict[str, Any]], None] | None = None) -> list[dict[str, Any]]:
    """编译 prompt.md（文件即接口：读 context/answers/skills），并返回分段索引。

    skills_text：多 skill 选用的「输出规范/反例」注入文本（core.skills.for_compile），
    与 task 目录单一 skills.md 合并后一起喂给 strong 模型。
    on_cost：计费回调（审计 R3，透传 LLMClient）。
    返回的段列表由调用方写入 state.json.segments；
    是否逐段喂入只由 dispatch.resolve_segment_mode 单点判定（审计 R5，全链唯一源）。
    """
    task_dir = Path(task_dir)
    llm = LLMClient(config, on_cost=on_cost)
    merged_skills = "\n\n".join(p for p in (_read(task_dir, "skills.md"), skills_text) if p.strip())
    user = (
        f"上下文：\n{_read(task_dir, 'context.md')}\n\n"
        f"用户确认答案：\n{_read(task_dir, 'answers.json')}\n\n"
        f"全局技能/约束：\n{merged_skills}"
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]
    prompt_md = llm.chat(messages, tier="strong", purpose="compile").strip() + "\n"

    (task_dir / "prompt.md").write_text(prompt_md, encoding="utf-8")

    # 审计 R5：此处曾有第二套分段阈值（compile.segment_threshold_chars）的死分支，
    # 与 dispatch 判定不一致；已删除，分段判定单点归 dispatch.resolve_segment_mode。
    return parse_segments(prompt_md)
