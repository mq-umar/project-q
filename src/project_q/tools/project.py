from __future__ import annotations

from typing import Any

from project_q.tools.base import ToolDefinition


class ProjectPlanBuildTool:
    definition = ToolDefinition(
        tool_id="project.plan_build",
        name="Plan Project Build",
        description="Classify and track a software, website, script, API, research, or repair dispatch",
        tier=1,
    )

    def __init__(self, dispatch_service) -> None:
        self.dispatch_service = dispatch_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        instruction = str(payload.get("instruction") or payload.get("prompt") or "").strip()
        if not instruction:
            raise ValueError("project.plan_build requires an instruction")
        target_dir = str(payload.get("target_dir", "")).strip()
        dispatch = self.dispatch_service.plan(instruction, target_dir=target_dir)
        return {
            "status": dispatch["status"],
            "dispatch_id": dispatch["id"],
            "task_type": dispatch["task_type"],
            "project_name": dispatch["project_name"],
            "project_path": dispatch["project_path"],
            "summary": dispatch["summary"],
            "acceptance_criteria": dispatch["acceptance_criteria"],
            "refined_prompt": dispatch["refined_prompt"],
        }
