"""orchestrator.adapters.claude_code
Anthropic Claude Code CLI 适配器（薄层）。

模板：claude -p {prompt_inline}（见 config.yaml adapters.claude_code）。
claude -p 输出纯文本而非 diff 机读格式，files_changed 由基类 git diff 前后推断兜底。
"""
from __future__ import annotations

from typing import Any

from .base import AgentAdapter


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude_code"


def build(config: dict[str, Any]) -> ClaudeCodeAdapter:
    return ClaudeCodeAdapter(config)
