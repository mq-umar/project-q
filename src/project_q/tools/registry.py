from __future__ import annotations

from pathlib import Path
from typing import Any

from project_q.tools.base import Tool
from project_q.tools.browser import BrowserActionsTool, BrowserGoalTool, BrowserInspectTool
from project_q.tools.codegen import ProjectGeneratorTool, WebsiteGeneratorTool
from project_q.tools.filesystem import (
    FilesystemAllowedRootsTool,
    FilesystemListTool,
    FilesystemOpenFileChoiceTool,
    FilesystemReadTool,
    FilesystemResolveFileRequestTool,
    FilesystemSearchTool,
    FilesystemWriteTool,
)
from project_q.tools.shell import ShellCommandTool
from project_q.tools.spreadsheet import SpreadsheetAnalyzeTool, SpreadsheetInspectTool, SpreadsheetWriteAnalysisTool
from project_q.tools.windows import (
    WindowsActivateWindowTool,
    WindowsLaunchApplicationTool,
    WindowsListWindowsTool,
    WindowsOpenUrlTool,
    WindowsSendKeysTool,
)


class ToolRegistry:
    def __init__(self, workspace_root: Path, data_root: Path, settings_service=None) -> None:
        self.tools: dict[str, Tool] = {}
        for tool in (
            FilesystemListTool(workspace_root, data_root, settings_service),
            FilesystemReadTool(workspace_root, data_root, settings_service),
            FilesystemWriteTool(workspace_root, data_root, settings_service),
            FilesystemSearchTool(workspace_root, data_root, settings_service),
            FilesystemResolveFileRequestTool(workspace_root, data_root, settings_service),
            FilesystemOpenFileChoiceTool(workspace_root, data_root, settings_service),
            FilesystemAllowedRootsTool(workspace_root, data_root, settings_service),
            SpreadsheetInspectTool(workspace_root, data_root, settings_service),
            SpreadsheetAnalyzeTool(workspace_root, data_root, settings_service),
            SpreadsheetWriteAnalysisTool(workspace_root, data_root, settings_service),
            WebsiteGeneratorTool(workspace_root),
            ProjectGeneratorTool(workspace_root),
            ShellCommandTool(workspace_root, settings_service),
            BrowserInspectTool(data_root, settings_service),
            BrowserActionsTool(data_root, settings_service),
            BrowserGoalTool(data_root, settings_service),
            WindowsListWindowsTool(),
            WindowsLaunchApplicationTool(workspace_root),
            WindowsOpenUrlTool(),
            WindowsActivateWindowTool(),
            WindowsSendKeysTool(),
        ):
            self.tools[tool.definition.tool_id] = tool

    def describe_all(self) -> list[dict[str, Any]]:
        return [
            {
                "tool_id": tool.definition.tool_id,
                "name": tool.definition.name,
                "description": tool.definition.description,
                "tier": tool.definition.tier,
            }
            for tool in self.tools.values()
        ]

    def register(self, tool: Tool) -> None:
        self.tools[tool.definition.tool_id] = tool

    def get(self, tool_id: str) -> Tool:
        try:
            return self.tools[tool_id]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {tool_id}") from exc
