"""orchestrator.adapters.qoder
Qoder coding agent 适配器（薄层，模板与 capabilities 见 config.yaml adapters.qoder）。
"""
from __future__ import annotations

from typing import Any

from .base import AgentAdapter


class QoderAdapter(AgentAdapter):
    name = "qoder"


def build(config: dict[str, Any]) -> QoderAdapter:
    return QoderAdapter(config)
