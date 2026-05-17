from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(slots=True)
class ToolDefinition:
    tool_id: str
    name: str
    description: str
    tier: int


class Tool(Protocol):
    definition: ToolDefinition

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...

