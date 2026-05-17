from __future__ import annotations

from typing import Any

from project_q.models import utc_now
from project_q.storage import Database


class AuditService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def log(
        self,
        *,
        action_type: str,
        action_tier: int,
        tool_name: str,
        outcome: str,
        approved_by_owner: bool = False,
        input_sources: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        model: str = "local-mvp",
        error: str = "",
    ) -> str:
        entry_id = self.db.make_id("audit")
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (
                    id, timestamp, action_type, action_tier, tool_name, model,
                    input_sources_json, approved_by_owner, outcome, error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    utc_now(),
                    action_type,
                    action_tier,
                    tool_name,
                    model,
                    self.db.dumps(input_sources or []),
                    1 if approved_by_owner else 0,
                    outcome,
                    error,
                    self.db.dumps(metadata or {}),
                ),
            )
        return entry_id

    def list_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM audit_log
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "action_type": row["action_type"],
                "action_tier": row["action_tier"],
                "tool_name": row["tool_name"],
                "model": row["model"],
                "input_sources": self.db.loads(row["input_sources_json"]),
                "approved_by_owner": bool(row["approved_by_owner"]),
                "outcome": row["outcome"],
                "error": row["error"],
                "metadata": self.db.loads(row["metadata_json"]),
            }
            for row in rows
        ]

