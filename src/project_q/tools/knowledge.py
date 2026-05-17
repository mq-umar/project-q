from __future__ import annotations

from typing import Any

from project_q.tools.base import ToolDefinition


class KnowledgeAnswerTool:
    definition = ToolDefinition(
        tool_id="knowledge.answer",
        name="Knowledge Workbench",
        description="Answer writing, math, physics, history, quant, science, and general problem-solving requests",
        tier=0,
    )

    def __init__(self, knowledge_service) -> None:
        self.knowledge_service = knowledge_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        instruction = str(payload.get("instruction", "")).strip()
        if not instruction:
            instruction = str(payload.get("prompt", "")).strip()
        if not instruction:
            raise ValueError("knowledge.answer requires an instruction")
        depth = str(payload.get("depth", "standard")).strip() or "standard"
        style = str(payload.get("style", "")).strip()
        return self.knowledge_service.answer(instruction, depth=depth, style=style)
