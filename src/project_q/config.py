from __future__ import annotations

import os
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
    relay_base_url: str = ""
    relay_bootstrap_token: str = ""
    worker_mode: bool = False

    @classmethod
    def discover(cls, workspace_root: Path | None = None) -> "AppConfig":
        resolved_workspace = (workspace_root or Path.cwd()).resolve()
        data_root = resolved_workspace / ".project_q"
        db_path = data_root / "project_q.db"
        host = os.environ.get("PROJECT_Q_HOST", "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(os.environ.get("PROJECT_Q_PORT", "8787"))
        except ValueError as exc:
            raise ValueError("PROJECT_Q_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("PROJECT_Q_PORT must be between 1 and 65535")
        return cls(
            project_name="Project Q",
            workspace_root=resolved_workspace,
            data_root=data_root,
            db_path=db_path,
            host=host,
            port=port,
            relay_base_url=os.environ.get("PROJECT_Q_RELAY_URL", "").strip(),
            relay_bootstrap_token=os.environ.get(
                "PROJECT_Q_RELAY_BOOTSTRAP_TOKEN",
                "",
            ).strip(),
            worker_mode=os.environ.get("PROJECT_Q_WORKER_MODE", "").strip().lower()
            in {"1", "true", "yes", "on"},
        )
