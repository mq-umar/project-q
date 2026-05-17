from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from project_q.tools.access import resolve_allowed_path
from project_q.tools.base import ToolDefinition


class ShellCommandTool:
    definition = ToolDefinition(
        tool_id="shell.run_command",
        name="Run Shell Command",
        description="Run a PowerShell command within the workspace or owner-configured roots",
        tier=2,
    )

    def __init__(self, workspace_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = payload["command"]
        workdir_value = payload.get("workdir", ".")
        timeout_seconds = min(int(payload.get("timeout_seconds", 15)), 30)
        workdir = resolve_allowed_path(
            workdir_value,
            workspace_root=self.workspace_root,
            settings_service=self.settings_service,
            must_exist=True,
        )
        if not workdir.is_dir():
            raise NotADirectoryError(str(workdir))
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "command": command,
            "workdir": str(workdir),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
