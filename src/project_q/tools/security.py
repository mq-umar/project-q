from __future__ import annotations

from typing import Any

from project_q.tools.base import ToolDefinition


class SecurityScanExternalContentTool:
    definition = ToolDefinition(
        tool_id="security.scan_external_content",
        name="Scan External Content",
        description="Label external content trust, detect prompt-injection patterns, and produce safe context",
        tier=0,
    )

    def __init__(self, trust_service) -> None:
        self.trust_service = trust_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.trust_service.scan_external_content(
            content=str(payload.get("content", "")),
            source_type=str(payload.get("source_type", "external")),
            origin_identifier=str(payload.get("origin_identifier", "")),
        )
