from __future__ import annotations

from pathlib import Path
from typing import Any

from project_q.tools.base import Tool
from project_q.tools.browser import BrowserActionsTool, BrowserGoalTool, BrowserInspectTool
from project_q.tools.codegen import ProjectGeneratorTool, WebsiteGeneratorTool
from project_q.tools.filesystem import (
    FilesystemAllowedRootsTool,
    FilesystemListTool,
    FilesystemMoveTool,
    FilesystemOpenFileChoiceTool,
    FilesystemReadTool,
    FilesystemResolveFileRequestTool,
    FilesystemSearchTool,
    FilesystemWatchPollTool,
    FilesystemWatchStartTool,
    FilesystemWriteTool,
    FilesystemZipTool,
)
from project_q.tools.productivity import CalendarInviteTool, EmailDraftTool
from project_q.tools.shell import ShellCommandTool
from project_q.tools.spreadsheet import SpreadsheetAnalyzeTool, SpreadsheetInspectTool, SpreadsheetWriteAnalysisTool
from project_q.tools.voice import VoiceListenOnceTool, VoiceSpeakTool
from project_q.tools.windows import (
    WindowsActivateWindowTool,
    WindowsAppStateTool,
    WindowsCaptureScreenshotTool,
    WindowsClipboardReadTool,
    WindowsClipboardWriteTool,
    WindowsFocusFollowTool,
    WindowsInspectUITreeTool,
    WindowsInvokeUIElementTool,
    WindowsLaunchApplicationTool,
    WindowsListWindowsTool,
    WindowsNotificationTool,
    WindowsOcrScreenshotTool,
    WindowsOpenUrlTool,
    WindowsRegistryReadTool,
    WindowsRegistryWriteTool,
    WindowsScreenshotDiffTool,
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
            FilesystemMoveTool(workspace_root, data_root, settings_service),
            FilesystemZipTool(workspace_root, data_root, settings_service),
            FilesystemWatchStartTool(workspace_root, data_root, settings_service),
            FilesystemWatchPollTool(workspace_root, data_root, settings_service),
            FilesystemResolveFileRequestTool(workspace_root, data_root, settings_service),
            FilesystemOpenFileChoiceTool(workspace_root, data_root, settings_service),
            FilesystemAllowedRootsTool(workspace_root, data_root, settings_service),
            SpreadsheetInspectTool(workspace_root, data_root, settings_service),
            SpreadsheetAnalyzeTool(workspace_root, data_root, settings_service),
            SpreadsheetWriteAnalysisTool(workspace_root, data_root, settings_service),
            EmailDraftTool(data_root),
            CalendarInviteTool(data_root),
            WebsiteGeneratorTool(workspace_root),
            ProjectGeneratorTool(workspace_root),
            ShellCommandTool(workspace_root, settings_service),
            BrowserInspectTool(data_root, settings_service),
            BrowserActionsTool(
                data_root,
                settings_service,
                workspace_root=workspace_root,
            ),
            BrowserGoalTool(data_root, settings_service),
            VoiceSpeakTool(settings_service),
            VoiceListenOnceTool(settings_service),
            WindowsListWindowsTool(),
            WindowsLaunchApplicationTool(workspace_root),
            WindowsOpenUrlTool(),
            WindowsNotificationTool(settings_service),
            WindowsActivateWindowTool(),
            WindowsSendKeysTool(),
            WindowsClipboardReadTool(),
            WindowsClipboardWriteTool(),
            WindowsCaptureScreenshotTool(data_root),
            WindowsOcrScreenshotTool(data_root),
            WindowsScreenshotDiffTool(data_root),
            WindowsAppStateTool(),
            WindowsFocusFollowTool(),
            WindowsInspectUITreeTool(),
            WindowsInvokeUIElementTool(),
            WindowsRegistryReadTool(),
            WindowsRegistryWriteTool(),
        ):
            self.tools[tool.definition.tool_id] = tool

        # Git tools (conditionally loaded)
        try:
            from project_q.tools.git_tools import (
                GitStatusTool, GitInitTool, GitAddTool, GitCommitTool,
                GitPushTool, GitPullTool, GitDiffTool, GitBranchTool,
                GitLogTool, GitCloneTool,
            )
            for git_tool in (
                GitStatusTool(workspace_root, settings_service),
                GitInitTool(workspace_root, settings_service),
                GitAddTool(workspace_root, settings_service),
                GitCommitTool(workspace_root, settings_service),
                GitPushTool(workspace_root, settings_service),
                GitPullTool(workspace_root, settings_service),
                GitDiffTool(workspace_root, settings_service),
                GitBranchTool(workspace_root, settings_service),
                GitLogTool(workspace_root, settings_service),
                GitCloneTool(workspace_root, settings_service),
            ):
                self.tools[git_tool.definition.tool_id] = git_tool
        except ImportError:
            pass

        # Outlook tools (conditionally loaded)
        try:
            from project_q.tools.outlook import (
                OutlookEmailListTool, OutlookEmailReadTool, OutlookEmailSendTool,
                OutlookCalendarListTool, OutlookCalendarCreateTool,
            )
            for outlook_tool in (
                OutlookEmailListTool(settings_service),
                OutlookEmailReadTool(settings_service),
                OutlookEmailSendTool(settings_service),
                OutlookCalendarListTool(settings_service),
                OutlookCalendarCreateTool(settings_service),
            ):
                self.tools[outlook_tool.definition.tool_id] = outlook_tool
        except ImportError:
            pass

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
