from __future__ import annotations

from typing import Any

from project_q.models import MemoryCreate, MemoryUpdate, utc_now
from project_q.storage import Database


class MemoryService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, payload: MemoryCreate) -> dict[str, Any]:
        memory_id = self.db.make_id("mem")
        now = utc_now()
        record = payload.model_dump()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO memories (
                    id, text, kind, source, confidence, owner_confirmed,
                    tags_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    record["text"],
                    record["kind"],
                    record["source"],
                    record["confidence"],
                    1 if record["owner_confirmed"] else 0,
                    self.db.dumps(record["tags"]),
                    self.db.dumps(record["metadata"]),
                    now,
                    now,
                ),
            )
        return self.get(memory_id)

    def list_all(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM memories
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get(self, memory_id: str) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return self._row_to_dict(row)

    def update(self, memory_id: str, payload: MemoryUpdate) -> dict[str, Any]:
        current = self.get(memory_id)
        updated = {**current, **payload.model_dump(exclude_none=True)}
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE memories
                SET text = ?, kind = ?, confidence = ?, owner_confirmed = ?,
                    tags_json = ?, metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated["text"],
                    updated["kind"],
                    updated["confidence"],
                    1 if updated["owner_confirmed"] else 0,
                    self.db.dumps(updated["tags"]),
                    self.db.dumps(updated["metadata"]),
                    utc_now(),
                    memory_id,
                ),
            )
        return self.get(memory_id)

    def delete(self, memory_id: str) -> None:
        with self.db.connection() as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        lowered = f"%{query.lower()}%"
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM memories
                WHERE LOWER(text) LIKE ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (lowered, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "text": row["text"],
            "kind": row["kind"],
            "source": row["source"],
            "confidence": row["confidence"],
            "owner_confirmed": bool(row["owner_confirmed"]),
            "tags": self.db.loads(row["tags_json"]),
            "metadata": self.db.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

