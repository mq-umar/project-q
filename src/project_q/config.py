from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    project_name: str
    workspace_root: Path
    data_root: Path
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8787

    @classmethod
    def discover(cls, workspace_root: Path | None = None) -> "AppConfig":
        resolved_workspace = (workspace_root or Path.cwd()).resolve()
        data_root = resolved_workspace / ".project_q"
        db_path = data_root / "project_q.db"
        return cls(
            project_name="Project Q",
            workspace_root=resolved_workspace,
            data_root=data_root,
            db_path=db_path,
        )

