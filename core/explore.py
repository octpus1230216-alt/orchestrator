"""orchestrator.core.explore
阶段 1：对话挖掘需求。

输入：一句话需求 + 选用 skill（skills.md 全局复用能力/约束库）。
调用 fast 模型，要求返回结构化结果 {context, questions}，产出：
    context.md      —— 澄清后的"要干什么"
    questions.json  —— 结构化待确认问题（可勾选 / 可默认）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .llm import LLMClient

_SYSTEM = (
    "你是需求挖掘助手。基于「一句话需求」和给定的全局技能/约束，"
    "产出结构化澄清结果。必须只返回 JSON，形如："
    '{"context": {"goal": str, "background": str, "constraints": [str], '
    '"out_of_scope": [str]}, '
    '"questions": [{"id": str, "type": "choice|yesno|text", "question": str, '
    '"options": [str]?, "default": any?}]}。'
    "问题要覆盖影响实现方向的关键歧义，每题尽量给出可选答案与默认值。"
)


def _load_skills(task_dir: Path) -> str:
    """读取本任务 skills.md（单一技能文件兼容路径；不存在则空串）。"""
    f = Path(task_dir) / "skills.md"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def _skill_block(task_dir: Path, skills_text: str) -> str:
    """合并 task 目录 skills.md 与多 skill 选用结果（约束/偏好分区）。"""
    parts = [p for p in (_load_skills(task_dir), skills_text or "") if p.strip()]
    return "\n\n".join(parts)


def _render_context_md(context: dict[str, Any]) -> str:
    lines = ["# 任务上下文 (context.md)", ""]
    lines += [f"## 目标\n{context.get('goal', '')}", ""]
    if context.get("background"):
        lines += ["## 背景", context["background"], ""]
    for key, title in (("constraints", "约束"), ("out_of_scope", "超出范围")):
        items = context.get(key) or []
        if items:
            lines += [f"## {title}"] + [f"- {i}" for i in items] + [""]
    return "\n".join(lines).rstrip() + "\n"


def run(task_dir: Path, raw_requirement: str, config: dict[str, Any],
        skills_text: str = "",
        on_cost: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """驱动需求挖掘，写出 context.md 与 questions.json，返回结构化需求。

    skills_text：多 skill 选用的「约束/偏好」注入文本（core.skills.for_explore），
    与 task 目录单一 skills.md 合并后一起喂给 fast 模型。
    on_cost：计费回调（审计 R3，透传 LLMClient，每次调用一条记录）。
    """
    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)

    llm = LLMClient(config, on_cost=on_cost)
    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": f"一句话需求：{raw_requirement}\n\n全局技能/约束：\n{_skill_block(task_dir, skills_text)}",
        },
    ]
    data = llm.chat_json(messages, tier="fast", purpose="explore")

    context = data.get("context", {})
    questions = data.get("questions", [])

    (task_dir / "context.md").write_text(_render_context_md(context), encoding="utf-8")
    (task_dir / "questions.json").write_text(
        json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"context": context, "questions": questions}
