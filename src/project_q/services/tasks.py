from __future__ import annotations

from typing import Any

from project_q.models import TaskCreate, TaskUpdate, utc_now
from project_q.storage import Database


class TaskService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, payload: TaskCreate) -> dict[str, Any]:
        task_id = self.db.make_id("task")
        now = utc_now()
        record = payload.model_dump()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, title, description, priority, status, due_at,
                    source, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    record["title"],
                    record["description"],
                    record["priority"],
                    record["status"],
                    record["due_at"],
                    record["source"],
                    self.db.dumps(record["metadata"]),
                    now,
                    now,
                ),
            )
        return self.get(task_id)

    def list_all(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM tasks
                ORDER BY
                    CASE status
                        WHEN 'in_progress' THEN 0
                        WHEN 'pending' THEN 1
                        WHEN 'blocked' THEN 2
                        ELSE 3
                    END,
                    priority ASC,
                    updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get(self, task_id: str) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._row_to_dict(row)

    def update(self, task_id: str, payload: TaskUpdate) -> dict[str, Any]:
        current = self.get(task_id)
        updated = {**current, **payload.model_dump(exclude_none=True)}
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET title = ?, description = ?, priority = ?, status = ?,
                    due_at = ?, metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated["title"],
                    updated["description"],
                    updated["priority"],
                    updated["status"],
                    updated["due_at"],
                    self.db.dumps(updated["metadata"]),
                    utc_now(),
                    task_id,
                ),
            )
        return self.get(task_id)

    def delete(self, task_id: str) -> None:
        with self.db.connection() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "priority": row["priority"],
            "status": row["status"],
            "due_at": row["due_at"],
            "source": row["source"],
            "metadata": self.db.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

