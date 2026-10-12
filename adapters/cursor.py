"""orchestrator.adapters.cursor
Cursor coding agent 适配器（薄层）。

模板：cursor-agent run --file {prompt_file}（见 config.yaml adapters.cursor）。
Cursor CLI 未安装时基类 run() 抛 AgentUnavailableError 并提示安装。
"""
from __future__ import annotations

from typing import Any

from .base import AgentAdapter


class CursorAdapter(AgentAdapter):
    name = "cursor"


def build(config: dict[str, Any]) -> CursorAdapter:
    return CursorAdapter(config)
