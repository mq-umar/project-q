from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from project_q.tools.base import ToolDefinition


def _run_git(args: list[str], *, cwd: Path, timeout: int = 60) -> str:
    """Run a git command in cwd, return stdout. Raise RuntimeError on non-zero exit."""
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "git command failed")
    return completed.stdout


def _effective_workspace_root(workspace_root: Path, settings_service=None) -> Path:
    configured = ""
    if settings_service is not None:
        configured = str(settings_service.get_all().get("git_workspace", "") or "").strip()
    if not configured:
        return workspace_root.resolve()
    configured_path = Path(configured)
    if configured_path.is_absolute():
        return configured_path.resolve()
    return (workspace_root / configured_path).resolve()


def _resolve_path(workspace_root: Path, raw: str, settings_service=None) -> Path:
    base = _effective_workspace_root(workspace_root, settings_service)
    value = str(raw or ".")
    candidate = Path(value)
    target = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    if not target.is_relative_to(base):
        raise PermissionError("git path is outside the configured git workspace")
    return target


# ---------------------------------------------------------------------------
# 1. GitStatusTool
# ---------------------------------------------------------------------------

class GitStatusTool:
    definition = ToolDefinition(
        tool_id="git.status",
        name="Git Status",
        description="Show the working-tree status of a git repository",
        tier=0,
    )

    def __init__(self, workspace_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        cwd = _resolve_path(self.workspace_root, payload.get("path", "."), self.settings_service)

        branch_out = _run_git(["branch", "--show-current"], cwd=cwd)
        branch = branch_out.strip()

        status_out = _run_git(["status", "--short", "--porcelain"], cwd=cwd)
        changes: list[dict[str, str]] = []
        for line in status_out.splitlines():
            if len(line) >= 3:
                status = line[:2].strip()
                file_path = line[3:]
                changes.append({"status": status, "path": file_path})

        return {
            "branch": branch,
            "changes": changes,
            "clean": len(changes) == 0,
        }


# ---------------------------------------------------------------------------
# 2. GitInitTool
# ---------------------------------------------------------------------------

class GitInitTool:
    definition = ToolDefinition(
        tool_id="git.init",
        name="Git Init",
        description="Initialise a new git repository, optionally creating an initial commit",
        tier=2,
    )

    def __init__(self, workspace_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        cwd = _resolve_path(self.workspace_root, payload.get("path", "."), self.settings_service)
        cwd.mkdir(parents=True, exist_ok=True)

        _run_git(["init"], cwd=cwd)

        message = f"Initialised empty git repository in {cwd}"

        if payload.get("initial_commit", False):
            _run_git(["add", "."], cwd=cwd)
            try:
                _run_git(["commit", "--allow-empty", "-m", "Initial commit"], cwd=cwd)
                message = f"Initialised repository and created initial commit in {cwd}"
            except RuntimeError:
                # nothing staged — still counts as initialised
                pass

        return {
            "path": str(cwd),
            "initialized": True,
            "message": message,
        }


# ---------------------------------------------------------------------------
# 3. GitAddTool
# ---------------------------------------------------------------------------

class GitAddTool:
    definition = ToolDefinition(
        tool_id="git.add",
        name="Git Add",
        description="Stage files for a git commit",
        tier=1,
    )

    def __init__(self, workspace_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        cwd = _resolve_path(self.workspace_root, payload.get("path", "."), self.settings_service)
        files: list[str] = payload.get("files") or ["."]
        if isinstance(files, str):
            files = [files]

        _run_git(["add", *files], cwd=cwd)

        # Count staged files
        status_out = _run_git(["status", "--short", "--porcelain"], cwd=cwd)
        staged_count = sum(1 for line in status_out.splitlines() if line and line[0] != " " and line[0] != "?")

        return {
            "staged_count": staged_count,
            "message": f"Staged {staged_count} file(s)",
        }


# ---------------------------------------------------------------------------
# 4. GitCommitTool
# ---------------------------------------------------------------------------

class GitCommitTool:
    definition = ToolDefinition(
        tool_id="git.commit",
        name="Git Commit",
        description="Create a git commit with a message",
        tier=2,
    )

    def __init__(self, workspace_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        cwd = _resolve_path(self.workspace_root, payload.get("path", "."), self.settings_service)
        message = payload.get("message", "").strip()
        if not message:
            raise ValueError("message is required")

        commit_out = _run_git(["commit", "-m", message], cwd=cwd)

        # Extract short hash from first line, e.g. "[main abc1234] message"
        commit_hash = ""
        files_changed = 0
        for line in commit_out.splitlines():
            if line.startswith("["):
                parts = line.split()
                if len(parts) >= 2:
                    commit_hash = parts[1].rstrip("]")
            if "changed" in line:
                try:
                    files_changed = int(line.strip().split()[0])
                except (ValueError, IndexError):
                    pass

        return {
            "commit_hash": commit_hash,
            "message": message,
            "files_changed": files_changed,
        }


# ---------------------------------------------------------------------------
# 5. GitPushTool
# ---------------------------------------------------------------------------

class GitPushTool:
    definition = ToolDefinition(
        tool_id="git.push",
        name="Git Push",
        description="Push commits to a remote git repository",
        tier=3,
    )

    def __init__(self, workspace_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        cwd = _resolve_path(self.workspace_root, payload.get("path", "."), self.settings_service)
        remote = str(payload.get("remote", "origin") or "origin")
        branch = str(payload.get("branch", "") or "").strip()

        if not branch:
            branch = _run_git(["branch", "--show-current"], cwd=cwd).strip()
        if not branch:
            raise ValueError("could not determine current branch; specify branch explicitly")

        _run_git(["push", remote, branch], cwd=cwd)

        return {
            "pushed": True,
            "remote": remote,
            "branch": branch,
        }


# ---------------------------------------------------------------------------
# 6. GitPullTool
# ---------------------------------------------------------------------------

class GitPullTool:
    definition = ToolDefinition(
        tool_id="git.pull",
        name="Git Pull",
        description="Pull commits from a remote git repository",
        tier=2,
    )

    def __init__(self, workspace_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        cwd = _resolve_path(self.workspace_root, payload.get("path", "."), self.settings_service)
        remote = str(payload.get("remote", "origin") or "origin")
        branch = str(payload.get("branch", "") or "").strip()

        args = ["pull", remote]
        if branch:
            args.append(branch)

        pull_out = _run_git(args, cwd=cwd)

        return {
            "pulled": True,
            "changes": pull_out.strip(),
        }


# ---------------------------------------------------------------------------
# 7. GitDiffTool
# ---------------------------------------------------------------------------

class GitDiffTool:
    definition = ToolDefinition(
        tool_id="git.diff",
        name="Git Diff",
        description="Show changes between working tree and index, or staged changes",
        tier=0,
    )

    def __init__(self, workspace_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        cwd = _resolve_path(self.workspace_root, payload.get("path", "."), self.settings_service)
        staged = bool(payload.get("staged", False))
        file_filter = str(payload.get("file", "") or "").strip()

        args = ["diff"]
        if staged:
            args.append("--staged")
        if file_filter:
            args.extend(["--", file_filter])

        diff_out = _run_git(args, cwd=cwd)

        # Count files changed from diff --stat
        stat_args = ["diff", "--stat"]
        if staged:
            stat_args.append("--staged")
        if file_filter:
            stat_args.extend(["--", file_filter])
        stat_out = _run_git(stat_args, cwd=cwd)
        files_changed = 0
        for line in stat_out.splitlines():
            if "changed" in line:
                try:
                    files_changed = int(line.strip().split()[0])
                except (ValueError, IndexError):
                    pass

        return {
            "diff": diff_out,
            "files_changed": files_changed,
        }


# ---------------------------------------------------------------------------
# 8. GitBranchTool
# ---------------------------------------------------------------------------

class GitBranchTool:
    definition = ToolDefinition(
        tool_id="git.branch",
        name="Git Branch",
        description="List, create, checkout, or delete git branches",
        tier=2,
    )

    def __init__(self, workspace_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        cwd = _resolve_path(self.workspace_root, payload.get("path", "."), self.settings_service)
        action = str(payload.get("action", "list")).strip().lower()
        name = str(payload.get("name", "") or "").strip()

        current = _run_git(["branch", "--show-current"], cwd=cwd).strip()
        created = ""

        if action == "list":
            branches_out = _run_git(["branch", "--list"], cwd=cwd)
            branches = [line.lstrip("* ").strip() for line in branches_out.splitlines() if line.strip()]
        elif action == "create":
            if not name:
                raise ValueError("name is required for action='create'")
            _run_git(["branch", name], cwd=cwd)
            branches_out = _run_git(["branch", "--list"], cwd=cwd)
            branches = [line.lstrip("* ").strip() for line in branches_out.splitlines() if line.strip()]
            created = name
        elif action == "checkout":
            if not name:
                raise ValueError("name is required for action='checkout'")
            _run_git(["checkout", name], cwd=cwd)
            current = _run_git(["branch", "--show-current"], cwd=cwd).strip()
            branches_out = _run_git(["branch", "--list"], cwd=cwd)
            branches = [line.lstrip("* ").strip() for line in branches_out.splitlines() if line.strip()]
        elif action == "delete":
            if not name:
                raise ValueError("name is required for action='delete'")
            _run_git(["branch", "-d", name], cwd=cwd)
            branches_out = _run_git(["branch", "--list"], cwd=cwd)
            branches = [line.lstrip("* ").strip() for line in branches_out.splitlines() if line.strip()]
        else:
            raise ValueError(f"unknown action '{action}'; expected list|create|checkout|delete")

        return {
            "branches": branches,
            "current": current,
            "created": created,
        }


# ---------------------------------------------------------------------------
# 9. GitLogTool
# ---------------------------------------------------------------------------

class GitLogTool:
    definition = ToolDefinition(
        tool_id="git.log",
        name="Git Log",
        description="Show recent git commit history",
        tier=0,
    )

    def __init__(self, workspace_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        cwd = _resolve_path(self.workspace_root, payload.get("path", "."), self.settings_service)
        limit = max(1, min(int(payload.get("limit", 10)), 500))
        oneline = bool(payload.get("oneline", True))

        if oneline:
            log_out = _run_git(
                ["log", f"-{limit}", "--format=%H%x1f%an%x1f%ad%x1f%s", "--date=iso-strict"],
                cwd=cwd,
            )
            commits: list[dict[str, str]] = []
            for line in log_out.splitlines():
                parts = line.split("\x1f")
                if len(parts) == 4:
                    commits.append({
                        "hash": parts[0][:12],
                        "author": parts[1],
                        "date": parts[2],
                        "message": parts[3],
                    })
        else:
            log_out = _run_git(
                ["log", f"-{limit}", "--format=%H%x1f%an%x1f%ad%x1f%B%x1e", "--date=iso-strict"],
                cwd=cwd,
            )
            commits = []
            for entry in log_out.split("\x1e"):
                entry = entry.strip()
                if not entry:
                    continue
                parts = entry.split("\x1f", 3)
                if len(parts) == 4:
                    commits.append({
                        "hash": parts[0][:12],
                        "author": parts[1],
                        "date": parts[2],
                        "message": parts[3].strip(),
                    })

        return {"commits": commits}


# ---------------------------------------------------------------------------
# 10. GitCloneTool
# ---------------------------------------------------------------------------

class GitCloneTool:
    definition = ToolDefinition(
        tool_id="git.clone",
        name="Git Clone",
        description="Clone a remote git repository to a local path",
        tier=2,
    )

    def __init__(self, workspace_root: Path, settings_service=None) -> None:
        self.workspace_root = workspace_root
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url", "")).strip()
        if not url:
            raise ValueError("url is required")

        destination_raw = str(payload.get("destination", ".") or ".")
        destination = _resolve_path(self.workspace_root, destination_raw, self.settings_service)
        depth = int(payload.get("depth", 0))

        args = ["clone"]
        if depth > 0:
            args.extend(["--depth", str(depth)])
        args.extend([url, str(destination)])

        _run_git(args, cwd=_effective_workspace_root(self.workspace_root, self.settings_service), timeout=120)

        return {
            "path": str(destination),
            "cloned": True,
            "url": url,
        }
