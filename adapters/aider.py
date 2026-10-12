"""orchestrator.adapters.aider
Aider pair-programming CLI 适配器（薄层）。

模板：aider --message-file {prompt_file}（见 config.yaml adapters.aider）。
aider 在 git 仓库内直接改文件，files_changed 由基类 git diff 前后推断收集。
"""
from __future__ import annotations

from typing import Any

from .base import AgentAdapter


class AiderAdapter(AgentAdapter):
    name = "aider"


def build(config: dict[str, Any]) -> AiderAdapter:
    return AiderAdapter(config)
