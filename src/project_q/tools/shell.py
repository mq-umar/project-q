from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from project_q.tools.access import resolve_allowed_path
from project_q.tools.base import ToolDefinition


class ShellCommandTool:
    SANDBOX_IMAGE = "mcr.microsoft.com/powershell:7.4-ubuntu-22.04"

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
        command = str(payload["command"]).strip()
        if not command:
            raise ValueError("command is required")
        workdir_value = payload.get("workdir", ".")
        timeout_seconds = min(max(int(payload.get("timeout_seconds", 15)), 1), 30)
        workdir = resolve_allowed_path(
            workdir_value,
            workspace_root=self.workspace_root,
            settings_service=self.settings_service,
            must_exist=True,
        )
        if not workdir.is_dir():
            raise NotADirectoryError(str(workdir))

        settings = self.settings_service.get_all() if self.settings_service is not None else {}
        execution_environment = str(
            settings.get("execution_environment", "sandbox_first")
        ).strip()
        if execution_environment == "sandbox_first":
            return self._execute_in_docker(
                command=command,
                workdir=workdir,
                timeout_seconds=timeout_seconds,
            )
        if execution_environment == "direct_trusted":
            return self._execute_direct(
                command=command,
                workdir=workdir,
                timeout_seconds=timeout_seconds,
            )
        raise ValueError(
            "execution_environment must be `sandbox_first` or `direct_trusted`"
        )

    def _execute_in_docker(
        self,
        *,
        command: str,
        workdir: Path,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        docker = shutil.which("docker")
        if not docker:
            raise RuntimeError(
                "Docker is required for sandbox_first shell execution. "
                "Start Docker Desktop or explicitly select direct_trusted in Settings."
            )
        mount = f"{workdir.resolve()}:/workspace"
        completed = subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--network",
                "none",
                "--memory",
                "512m",
                "--cpus",
                "1",
                "--pids-limit",
                "128",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "65534:65534",
                "--workdir",
                "/workspace",
                "--volume",
                mount,
                self.SANDBOX_IMAGE,
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "command": command,
            "workdir": str(workdir),
            "backend": "docker",
            "sandboxed": True,
            "network": "none",
            "image": self.SANDBOX_IMAGE,
            "timeout_seconds": timeout_seconds,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    @staticmethod
    def _execute_direct(
        *,
        command: str,
        workdir: Path,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "command": command,
            "workdir": str(workdir),
            "backend": "direct_trusted",
            "sandboxed": False,
            "network": "host",
            "timeout_seconds": timeout_seconds,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
