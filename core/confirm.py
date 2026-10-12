"""orchestrator.core.confirm
阶段 2：确认。

rich 渲染 questions.json，支持逐题交互回答；`--yes` 全用默认。
结果写 answers.json（[{id, question, answer}, ...]）。
rich 不可用时自动降级为纯文本交互。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:  # rich 为可选依赖，缺失时降级
    from rich.console import Console
    from rich.prompt import Confirm, Prompt

    _HAS_RICH = True
except ImportError:  # pragma: no cover
    _HAS_RICH = False


class NonInteractiveError(RuntimeError):
    """无 TTY 且未 --yes：不 hang、不 traceback，由 CLI 捕获后以退出码 2 清晰退出（审计 R4）。"""


def _answer_default(q: dict[str, Any]) -> Any:
    """取问题默认值；无 default 时按类型给安全兜底。"""
    if "default" in q:
        return q["default"]
    return {"choice": (q.get("options") or [""])[0], "yesno": False, "text": ""}.get(
        q.get("type", "text"), ""
    )


def _prompt_one(q: dict[str, Any]) -> Any:
    default = _answer_default(q)
    qtype = q.get("type", "text")
    if _HAS_RICH:
        console = Console()
        console.print(f"[bold cyan]?[/] {q['question']}")
        if qtype == "choice":
            options = q.get("options", [])
            console.print("  " + " / ".join(f"[yellow]{i}[/]. {o}" for i, o in enumerate(options)))
            raw = Prompt.ask("  选择", choices=[str(i) for i in range(len(options))], default="0")
            return options[int(raw)] if options else default
        if qtype == "yesno":
            return Confirm.ask("  是/否", default=bool(default))
        return Prompt.ask("  回答", default=str(default))
    # 降级：纯文本
    ans = input(f"? {q['question']}（回车用默认 {default!r}）：").strip()
    if not ans:
        return default
    if qtype == "yesno":
        return ans.lower() in ("y", "yes", "true", "1", "是")
    if qtype == "choice" and ans.isdigit() and 0 <= int(ans) < len(q.get("options", [])):
        return q["options"][int(ans)]
    return ans


def run(task_dir: Path, config: dict[str, Any], *, assume_yes: bool = False) -> list[dict[str, Any]]:
    """读取 questions.json，产出 answers.json。assume_yes 时全部取默认。"""
    task_dir = Path(task_dir)
    questions = json.loads((task_dir / "questions.json").read_text(encoding="utf-8"))

    answers: list[dict[str, Any]] = []
    for q in questions:
        if assume_yes:
            value = _answer_default(q)
        else:
            try:
                value = _prompt_one(q)
            except EOFError:
                # stdin 已关闭（CI/管道空输入）：友好提示替代裸 traceback（审计 R4）
                raise NonInteractiveError(
                    "确认阶段需要交互输入，但当前是非交互环境（无 TTY）：非交互环境请使用 --yes。"
                ) from None
        answers.append({"id": q.get("id"), "question": q.get("question"), "answer": value})

    (task_dir / "answers.json").write_text(
        json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return answers
