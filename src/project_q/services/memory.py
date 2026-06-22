from __future__ import annotations

from typing import Any

from project_q.models import MemoryCreate, MemoryUpdate, utc_now
from project_q.storage import Database


class MemoryService:
    def __init__(self, db: Database, settings_service=None, sync_service=None) -> None:
        self.db = db
        self._settings_service = settings_service
        self.sync_service = sync_service

    def _memory_mode(self) -> str:
        if self._settings_service is None:
            return "standard"
        return str(self._settings_service.get_all().get("memory_mode", "standard"))

    def create(self, payload: MemoryCreate) -> dict[str, Any]:
        memory_id = self.db.make_id("mem")
        now = utc_now()
        record = payload.model_dump()

        # Ephemeral mode: return a fake in-session record without DB persistence
        if self._memory_mode() == "ephemeral" and record.get("source") != "import":
            return {
                "id": memory_id,
                "text": record["text"],
                "kind": record["kind"],
                "source": record["source"],
                "confidence": record["confidence"],
                "owner_confirmed": record["owner_confirmed"],
                "tags": record["tags"],
                "metadata": record["metadata"],
                "created_at": now,
                "updated_at": now,
            }

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
        created = self.get(memory_id)
        self._emit("created", created)
        return created

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

    def export_all(self, *, limit: int = 10000) -> dict[str, Any]:
        bounded_limit = min(max(int(limit), 1), 100000)
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM memories
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        memories = [self._row_to_dict(row) for row in rows]
        return {
            "version": 1,
            "exported_at": utc_now(),
            "memory_count": len(memories),
            "memories": memories,
        }

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
        record = self.get(memory_id)
        self._emit("updated", record)
        return record

    def delete(self, memory_id: str) -> None:
        current = self.get(memory_id)
        with self.db.connection() as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self._emit("deleted", {"id": memory_id, "source": current["source"]})

    def bulk_delete(
        self,
        *,
        source: str | None = None,
        kind: str | None = None,
        tag: str | None = None,
        older_than: str | None = None,
        dry_run: bool = True,
        owner_confirmed: bool = False,
        all_memories: bool = False,
    ) -> dict[str, Any]:
        normalized_source = str(source or "").strip()
        normalized_kind = str(kind or "").strip()
        normalized_tag = str(tag or "").strip()
        normalized_older_than = str(older_than or "").strip()
        has_filter = any([normalized_source, normalized_kind, normalized_tag, normalized_older_than, all_memories])
        if not has_filter:
            raise ValueError("bulk memory deletion requires at least one filter or all_memories=True")

        memories = self.export_all(limit=100000)["memories"]
        matched = [
            memory for memory in memories
            if self._memory_matches_filters(
                memory,
                source=normalized_source,
                kind=normalized_kind,
                tag=normalized_tag,
                older_than=normalized_older_than,
                all_memories=all_memories,
            )
        ]
        matched_ids = [memory["id"] for memory in matched]
        if dry_run:
            deleted_count = 0
        else:
            if not owner_confirmed:
                raise PermissionError("bulk memory deletion requires owner confirmation")
            with self.db.connection() as conn:
                conn.executemany("DELETE FROM memories WHERE id = ?", [(mid,) for mid in matched_ids])
            deleted_count = len(matched_ids)
        return {
            "dry_run": dry_run,
            "matched_count": len(matched_ids),
            "deleted_count": deleted_count,
            "matched_memory_ids": matched_ids,
            "filters": {
                "source": normalized_source,
                "kind": normalized_kind,
                "tag": normalized_tag,
                "older_than": normalized_older_than,
                "all_memories": all_memories,
            },
        }

    def _emit(self, operation: str, payload: dict[str, Any]) -> None:
        if self.sync_service is not None:
            self.sync_service.append(
                resource_type="memory",
                resource_id=str(payload["id"]),
                operation=operation,
                payload=payload,
            )

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search memories using FTS5 when available, falling back to LIKE.

        Results are sorted by relevance: exact phrase matches rank highest,
        followed by FTS-ranked matches, then LIKE matches ordered by updated_at.
        """
        # ---- Try FTS5 first ----
        try:
            with self.db.connection() as conn:
                fts_rows = conn.execute(
                    """
                    SELECT m.*
                    FROM memories AS m
                    JOIN memories_fts AS fts ON fts.rowid = m.rowid
                    WHERE memories_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
            results = [self._row_to_dict(row) for row in fts_rows]
            # Apply relevance scoring: exact matches score 1.0, others score 0.6
            query_lower = query.lower()
            scored = []
            for mem in results:
                score = 1.0 if query_lower in mem["text"].lower() else 0.6
                scored.append((score, mem))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [mem for _, mem in scored]
        except Exception:  # noqa: BLE001
            pass  # FTS not available or query parse error — fall through

        # ---- Fallback: LIKE search with relevance scoring ----
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
        memories = [self._row_to_dict(row) for row in rows]

        # Score: exact phrase match → 1.0; starts-with match → 0.8; contains → 0.6
        query_lower = query.lower()
        scored = []
        for mem in memories:
            text_lower = mem["text"].lower()
            if text_lower == query_lower:
                score = 1.0
            elif query_lower in text_lower and text_lower.startswith(query_lower):
                score = 0.8
            else:
                score = 0.6
            scored.append((score, mem))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [mem for _, mem in scored]

    def prune_expired(self) -> dict[str, Any]:
        """Delete unconfirmed memories older than the configured retention window."""
        settings = self._settings_service.get_all() if self._settings_service else {}
        retention_days = int(settings.get("memory_retention_days", 90))
        if retention_days <= 0:
            return {"pruned": 0, "retention_days": retention_days, "skipped": "disabled"}
        from datetime import UTC, datetime, timedelta
        cutoff_dt = datetime.now(UTC) - timedelta(days=retention_days)
        # Match utc_now()'s 'Z'-suffixed format so the string comparison against
        # created_at is apples-to-apples (otherwise the suffix skews equality).
        cutoff_str = cutoff_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self.db.connection() as conn:
            result = conn.execute(
                "DELETE FROM memories WHERE created_at < ? AND owner_confirmed = 0",
                (cutoff_str,),
            )
            pruned = result.rowcount
        return {"pruned": pruned, "retention_days": retention_days, "cutoff": cutoff_str}

    def _memory_matches_filters(
        self,
        memory: dict[str, Any],
        *,
        source: str,
        kind: str,
        tag: str,
        older_than: str,
        all_memories: bool,
    ) -> bool:
        if all_memories:
            return True
        if source and memory["source"] != source:
            return False
        if kind and memory["kind"] != kind:
            return False
        if tag and tag not in memory.get("tags", []):
            return False
        if older_than and memory["updated_at"] >= older_than:
            return False
        return True

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
