from __future__ import annotations

from typing import Any

from project_q.tools.base import ToolDefinition


class DiagnosticsRunSelfCheckTool:
    definition = ToolDefinition(
        tool_id="diagnostics.run_self_check",
        name="Run Self Diagnostics",
        description="Run Project Q self-diagnostics, recent failure analysis, and code-generation simulations",
        tier=0,
    )

    def __init__(self, diagnostics_service) -> None:
        self.diagnostics_service = diagnostics_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = str(payload.get("source", "tool")).strip() or "tool"
        auto_repair_value = payload.get("auto_repair", True)
        auto_repair = str(auto_repair_value).strip().lower() not in {"0", "false", "no", "off"}
        return self.diagnostics_service.run(source=source, auto_repair=auto_repair)


class DiagnosticsAutoRepairTool:
    definition = ToolDefinition(
        tool_id="diagnostics.auto_repair",
        name="Auto Repair Diagnostics",
        description="Classify known Project Q failures, verify fixes, and resolve stale repair tasks",
        tier=1,
    )

    def __init__(self, self_repair_service) -> None:
        self.self_repair_service = self_repair_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = str(payload.get("source", "tool")).strip() or "tool"
        return self.self_repair_service.run(source=source)
