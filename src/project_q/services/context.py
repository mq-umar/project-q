from __future__ import annotations

from typing import Any


class ContextService:
    def __init__(
        self,
        db,
        memory_service,
        task_service,
        agent_service,
        routine_service,
        settings_service,
        tool_registry,
        workspace_root,
    ) -> None:
        self.db = db
        self.memory_service = memory_service
        self.task_service = task_service
        self.agent_service = agent_service
        self.routine_service = routine_service
        self.settings_service = settings_service
        self.tool_registry = tool_registry
        self.workspace_root = workspace_root

    def build(self, latest_message: str) -> dict[str, Any]:
        settings = self.settings_service.get_all()
        return {
            "latest_message": latest_message,
            "workspace_root": str(self.workspace_root),
            "owner": {
                "name": settings.get("owner_name", ""),
                "aggression_level": settings.get("aggression_level", "operator"),
                "memory_mode": settings.get("memory_mode", "standard"),
                "auto_approve_tier": settings.get("auto_approve_tier", 1),
                "file_access_roots": settings.get("file_access_roots", []),
                "learning_enabled": settings.get("learning_enabled", False),
            },
            "recent_messages": self._recent_messages(limit=8),
            "tasks": self.task_service.list_all(limit=8),
            "memories": self.memory_service.list_all(limit=8),
            "agents": self.agent_service.list_all(limit=6),
            "routines": self.routine_service.list_all(limit=6),
            "tools": self.tool_registry.describe_all(),
        }

    def summary_counts(self) -> dict[str, int]:
        return {
            "tasks": len(self.task_service.list_all(limit=500)),
            "agents": len(self.agent_service.list_all(limit=500)),
            "routines": len(self.routine_service.list_all(limit=500)),
            "memories": len(self.memory_service.list_all(limit=500)),
        }

    def _recent_messages(self, limit: int) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT role, content, created_at
                FROM conversations
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
