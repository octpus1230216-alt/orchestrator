"""orchestrator.adapters.trae
Trae coding agent 适配器（薄层）。

Trae 暂无可编程 CLI：config.yaml adapters.trae.command_template 留空，
调用 run() 时基类抛出 AgentUnavailableError 并附替代建议（清晰异常）。
后续接入 Trae 自动化接口时，只需在 config 填 {command_template}，本文件无需改动。
"""
from __future__ import annotations

from typing import Any

from .base import AgentAdapter


class TraeAdapter(AgentAdapter):
    name = "trae"


def build(config: dict[str, Any]) -> TraeAdapter:
    return TraeAdapter(config)
