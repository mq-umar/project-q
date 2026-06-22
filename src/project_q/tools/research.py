from __future__ import annotations

from typing import Any

from project_q.tools.base import ToolDefinition


class ResearchWebTool:
    definition = ToolDefinition(
        tool_id="research.web",
        name="Multi-source Web Research",
        description="Inspect and compare multiple public web sources with provenance and trust scanning",
        tier=1,
    )

    def __init__(self, service) -> None:
        self.service = service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        seed_urls = payload.get("seed_urls") or payload.get("urls") or []
        if isinstance(seed_urls, str):
            seed_urls = [seed_urls]
        return self.service.research(
            query=str(payload.get("query") or payload.get("instruction") or ""),
            seed_urls=list(seed_urls),
            max_sources=int(payload.get("max_sources", 4)),
        )
