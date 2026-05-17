from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

from project_q.tools.base import ToolDefinition


def _run_powershell_json(script: str, *, timeout_seconds: int = 10) -> Any:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "PowerShell command failed")
    stdout = completed.stdout.strip()
    return json.loads(stdout) if stdout else {}


class WindowsListWindowsTool:
    definition = ToolDefinition(
        tool_id="windows.list_windows",
        name="List Windows",
        description="List visible desktop windows and owning processes",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = min(max(int(payload.get("limit", 40)), 1), 120)
        script = f"""
        $windows = Get-Process |
            Where-Object {{ $_.MainWindowTitle -and $_.MainWindowTitle.Trim().Length -gt 0 }} |
            Sort-Object ProcessName |
            Select-Object -First {limit} Id, ProcessName, MainWindowTitle
        @{{ windows = @($windows) }} | ConvertTo-Json -Depth 4 -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10)


class WindowsLaunchApplicationTool:
    definition = ToolDefinition(
        tool_id="windows.launch_application",
        name="Launch Application",
        description="Launch a Windows application or executable",
        tier=2,
    )

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = payload.get("command") or payload.get("path")
        if not command:
            raise ValueError("command is required")
        args = [str(item) for item in payload.get("args", [])]
        workdir_value = payload.get("workdir")
        cwd = self._resolve_workdir(workdir_value)
        process = subprocess.Popen(
            [command, *args],
            cwd=cwd,
        )
        return {
            "command": command,
            "args": args,
            "workdir": str(cwd) if cwd else None,
            "pid": process.pid,
        }

    def _resolve_workdir(self, value: str | None) -> Path | None:
        if not value:
            return None
        raw = Path(value)
        if raw.is_absolute():
            return raw
        return (self.workspace_root / raw).resolve()


class WindowsActivateWindowTool:
    definition = ToolDefinition(
        tool_id="windows.activate_window",
        name="Activate Window",
        description="Bring a target window to the foreground by title or process id",
        tier=1,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        window_title = payload.get("window_title")
        process_id = payload.get("process_id")
        if not window_title and process_id is None:
            raise ValueError("window_title or process_id is required")
        title_literal = json.dumps(window_title or "")
        process_id_literal = "null" if process_id is None else json.dumps(int(process_id))
        script = f"""
        $windowTitle = {title_literal}
        $processId = {process_id_literal}
        $shell = New-Object -ComObject WScript.Shell
        if ($processId -ne $null) {{
            $process = Get-Process -Id $processId -ErrorAction Stop
            $activated = $shell.AppActivate($process.Id)
            $target = $process.MainWindowTitle
        }} else {{
            $activated = $shell.AppActivate($windowTitle)
            $target = $windowTitle
        }}
        @{{ activated = [bool]$activated; target = $target }} | ConvertTo-Json -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10)


class WindowsSendKeysTool:
    definition = ToolDefinition(
        tool_id="windows.send_keys",
        name="Send Keys",
        description="Send keystrokes to the active window or a target window",
        tier=2,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        keys = payload.get("keys")
        if not keys:
            raise ValueError("keys is required")
        window_title = payload.get("window_title")
        delay_ms = min(max(int(payload.get("delay_ms", 300)), 0), 5000)
        title_literal = json.dumps(window_title or "")
        keys_literal = json.dumps(str(keys))
        script = f"""
        $windowTitle = {title_literal}
        $keys = {keys_literal}
        $delayMs = {delay_ms}
        $shell = New-Object -ComObject WScript.Shell
        $activated = $true
        if ($windowTitle -and $windowTitle.Trim().Length -gt 0) {{
            $activated = $shell.AppActivate($windowTitle)
            Start-Sleep -Milliseconds $delayMs
        }}
        if (-not $activated) {{
            throw "Unable to activate target window."
        }}
        $shell.SendKeys($keys)
        @{{ sent = $true; target = $windowTitle; keys = $keys }} | ConvertTo-Json -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10)


class WindowsOpenUrlTool:
    definition = ToolDefinition(
        tool_id="windows.open_url",
        name="Open URL",
        description="Open a URL in the default browser and leave it visible",
        tier=1,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url", "")).strip()
        if not url:
            raise ValueError("url is required")
        script = f"""
        $url = {json.dumps(url)}
        Start-Process $url | Out-Null
        @{{ opened = $true; url = $url }} | ConvertTo-Json -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10)
