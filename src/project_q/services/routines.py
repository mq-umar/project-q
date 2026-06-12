from __future__ import annotations

from typing import Any

from project_q.models import RoutineCreate, RoutineUpdate, utc_now
from project_q.storage import Database


class RoutineService:
    def __init__(self, db: Database, sync_service=None) -> None:
        self.db = db
        self.sync_service = sync_service

    def create(self, payload: RoutineCreate) -> dict[str, Any]:
        routine_id = self.db.make_id("routine")
        now = utc_now()
        record = payload.model_dump()
        tools = self._normalize_tools(record["tools"], record["steps"])
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO routines (
                    id, name, goal, description, status, trigger_type,
                    trusted, tools_json, steps_json, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    routine_id,
                    record["name"],
                    record["goal"],
                    record["description"],
                    record["status"],
                    record["trigger_type"],
                    1 if record["trusted"] else 0,
                    self.db.dumps(tools),
                    self.db.dumps(record["steps"]),
                    record["notes"],
                    now,
                    now,
                ),
            )
        created = self.get(routine_id)
        self._emit("created", created)
        return created

    def list_all(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    routines.*,
                    (
                        SELECT created_at
                        FROM routine_runs
                        WHERE routine_runs.routine_id = routines.id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS last_run_at,
                    (
                        SELECT outcome
                        FROM routine_runs
                        WHERE routine_runs.routine_id = routines.id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS last_run_outcome
                FROM routines
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get(self, routine_id: str) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    routines.*,
                    (
                        SELECT created_at
                        FROM routine_runs
                        WHERE routine_runs.routine_id = routines.id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS last_run_at,
                    (
                        SELECT outcome
                        FROM routine_runs
                        WHERE routine_runs.routine_id = routines.id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS last_run_outcome
                FROM routines
                WHERE id = ?
                """,
                (routine_id,),
            ).fetchone()
        if row is None:
            raise KeyError(routine_id)
        return self._row_to_dict(row)

    def update(self, routine_id: str, payload: RoutineUpdate) -> dict[str, Any]:
        current = self.get(routine_id)
        updates = payload.model_dump(exclude_none=True)
        updated = {**current, **updates}
        tools = self._normalize_tools(updated["tools"], updated["steps"])
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE routines
                SET name = ?, goal = ?, description = ?, status = ?, trigger_type = ?,
                    trusted = ?, tools_json = ?, steps_json = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated["name"],
                    updated["goal"],
                    updated["description"],
                    updated["status"],
                    updated["trigger_type"],
                    1 if updated["trusted"] else 0,
                    self.db.dumps(tools),
                    self.db.dumps(updated["steps"]),
                    updated["notes"],
                    utc_now(),
                    routine_id,
                ),
            )
        record = self.get(routine_id)
        self._emit("updated", record)
        return record

    def delete(self, routine_id: str) -> None:
        with self.db.connection() as conn:
            conn.execute("DELETE FROM routines WHERE id = ?", (routine_id,))
        self._emit("deleted", {"id": routine_id})

    def _emit(self, operation: str, payload: dict[str, Any]) -> None:
        if self.sync_service is not None:
            self.sync_service.append(
                resource_type="routine",
                resource_id=str(payload["id"]),
                operation=operation,
                payload=payload,
            )

    def _normalize_tools(self, explicit_tools: list[str], steps: list[dict[str, Any]]) -> list[str]:
        tool_ids = list(explicit_tools)
        for step in steps:
            tool_id = step.get("tool_id")
            if tool_id and tool_id not in tool_ids:
                tool_ids.append(tool_id)
        return tool_ids

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "goal": row["goal"],
            "description": row["description"],
            "status": row["status"],
            "trigger_type": row["trigger_type"],
            "trusted": bool(row["trusted"]),
            "tools": self.db.loads(row["tools_json"]),
            "steps": self.db.loads(row["steps_json"]),
            "notes": row["notes"],
            "last_run_at": row["last_run_at"] if "last_run_at" in row.keys() else None,
            "last_run_outcome": row["last_run_outcome"] if "last_run_outcome" in row.keys() else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
