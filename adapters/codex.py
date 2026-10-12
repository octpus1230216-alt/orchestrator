"""orchestrator.adapters.codex
OpenAI Codex CLI 适配器（薄层）。

命令模板与 capabilities 均在 config.yaml adapters.codex 配置：
    codex exec --prompt-file {prompt_file}
变量替换 + subprocess 由基类 AgentAdapter 完成。
"""
from __future__ import annotations

from typing import Any

from .base import AgentAdapter


class CodexAdapter(AgentAdapter):
    name = "codex"


def build(config: dict[str, Any]) -> CodexAdapter:
    """工厂函数：dispatch 依据 config.yaml adapters.<name> 调用。"""
    return CodexAdapter(config)
