from __future__ import annotations

from typing import Any

from project_q.models import MemoryCreate, MemoryUpdate, utc_now
from project_q.services.embeddings import EmbeddingService, cosine_similarity
from project_q.storage import Database


class MemoryService:
    def __init__(
        self,
        db: Database,
        settings_service=None,
        sync_service=None,
        embedding_service: EmbeddingService | None = None,
        routine_service=None,
    ) -> None:
        self.db = db
        self._settings_service = settings_service
        self.sync_service = sync_service
        self._embedding_service = embedding_service or EmbeddingService(settings_service)
        # Optional dependency for promoting playbook candidates into routines.
        # Wired after construction (routines are built after memory in app.py).
        self.routine_service = routine_service

    def set_routine_service(self, routine_service) -> None:
        self.routine_service = routine_service

    def _memory_mode(self) -> str:
        if self._settings_service is None:
            return "standard"
        return str(self._settings_service.get_all().get("memory_mode", "standard"))

    def create(self, payload: MemoryCreate) -> dict[str, Any]:
        memory_id = self.db.make_id("mem")
        now = utc_now()
        record = payload.model_dump()
        # New depth fields default safely when the payload predates them.
        source_type = str(record.get("source_type") or "owner")
        trust_level = str(record.get("trust_level") or "trusted")
        inferred = bool(record.get("inferred") or False)
        evidence = record.get("evidence") or []

        # Best-effort embedding; silently None when Ollama is absent (default here).
        embedding = self._embedding_service.embed(record["text"])
        embedding_json = self.db.dumps(embedding) if embedding else ""

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
                "source_type": source_type,
                "trust_level": trust_level,
                "inferred": inferred,
                "evidence": evidence,
                "created_at": now,
                "updated_at": now,
            }

        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO memories (
                    id, text, kind, source, confidence, owner_confirmed,
                    tags_json, metadata_json, created_at, updated_at,
                    embedding_json, source_type, trust_level, inferred, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    embedding_json,
                    source_type,
                    trust_level,
                    1 if inferred else 0,
                    self.db.dumps(evidence),
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
        changes = payload.model_dump(exclude_none=True)
        updated = {**current, **changes}
        # Re-embed only when the text itself changed (best-effort, silent None).
        if "text" in changes and changes["text"] != current["text"]:
            new_embedding = self._embedding_service.embed(updated["text"])
            embedding_json = self.db.dumps(new_embedding) if new_embedding else ""
        else:
            embedding_json = None  # leave existing embedding untouched
        base_params = [
            updated["text"],
            updated["kind"],
            updated["confidence"],
            1 if updated["owner_confirmed"] else 0,
            self.db.dumps(updated["tags"]),
            self.db.dumps(updated["metadata"]),
            str(updated.get("source_type") or "owner"),
            str(updated.get("trust_level") or "trusted"),
            1 if updated.get("inferred") else 0,
            self.db.dumps(updated.get("evidence") or []),
        ]
        if embedding_json is None:
            # Leave the existing embedding untouched (text did not change).
            sql = (
                "UPDATE memories "
                "SET text = ?, kind = ?, confidence = ?, owner_confirmed = ?, "
                "tags_json = ?, metadata_json = ?, "
                "source_type = ?, trust_level = ?, inferred = ?, "
                "evidence_json = ?, updated_at = ? WHERE id = ?"
            )
            params = base_params + [utc_now(), memory_id]
        else:
            sql = (
                "UPDATE memories "
                "SET text = ?, kind = ?, confidence = ?, owner_confirmed = ?, "
                "tags_json = ?, metadata_json = ?, "
                "source_type = ?, trust_level = ?, inferred = ?, "
                "evidence_json = ?, embedding_json = ?, updated_at = ? WHERE id = ?"
            )
            params = base_params + [embedding_json, utc_now(), memory_id]
        with self.db.connection() as conn:
            conn.execute(sql, params)
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
        """Search memories with a lexical-first, optionally embedding-reranked path.

        The lexical layer (FTS5, falling back to LIKE) is always authoritative for
        which rows are candidates. When a query embedding is available (Ollama
        present) the candidates are re-ranked by blending the lexical relevance
        score with pure-Python cosine similarity. When embeddings are unavailable
        — the default in this environment — the lexical scores stand alone, so
        the existing FTS5/LIKE behavior is preserved exactly.
        """
        candidates = self._lexical_candidates(query, limit)
        if not candidates:
            return []

        # Best-effort query embedding; silent None → pure lexical ranking.
        query_embedding = self._embedding_service.embed(query)

        scored: list[tuple[float, dict[str, Any]]] = []
        for lexical_score, mem, row in candidates:
            final_score = lexical_score
            if query_embedding is not None:
                row_embedding = self._row_embedding(row)
                if row_embedding is not None:
                    similarity = cosine_similarity(query_embedding, row_embedding)
                    # Hybrid blend: lexical anchors recall, cosine refines ranking.
                    final_score = 0.5 * lexical_score + 0.5 * similarity
            scored.append((final_score, mem))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [mem for _, mem in scored]

    def _lexical_candidates(
        self, query: str, limit: int
    ) -> list[tuple[float, dict[str, Any], Any]]:
        """Return (lexical_score, memory_dict, raw_row) candidates via FTS5/LIKE."""
        query_lower = query.lower()

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
            candidates: list[tuple[float, dict[str, Any], Any]] = []
            for row in fts_rows:
                mem = self._row_to_dict(row)
                score = 1.0 if query_lower in mem["text"].lower() else 0.6
                candidates.append((score, mem, row))
            return candidates
        except Exception:  # noqa: BLE001
            pass  # FTS not available or query parse error — fall through

        # ---- Fallback: LIKE search with relevance scoring ----
        lowered = f"%{query_lower}%"
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
        candidates = []
        for row in rows:
            mem = self._row_to_dict(row)
            text_lower = mem["text"].lower()
            if text_lower == query_lower:
                score = 1.0
            elif query_lower in text_lower and text_lower.startswith(query_lower):
                score = 0.8
            else:
                score = 0.6
            candidates.append((score, mem, row))
        return candidates

    # ── Playbook candidates (PRD §6.2) ───────────────────────────────────────
    def list_playbook_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        """List procedural playbook-candidate memories awaiting owner promotion.

        These are written by learning.reflect_on_task() as kind='procedural',
        tagged 'playbook_candidate', with a metadata.tool_sequence. They are
        never auto-trusted; the owner promotes them into a trusted routine.
        """
        candidates: list[dict[str, Any]] = []
        for mem in self.list_all(limit=max(limit, 1) * 5):
            if mem.get("kind") != "procedural":
                continue
            if "playbook_candidate" not in (mem.get("tags") or []):
                continue
            metadata = mem.get("metadata") or {}
            tool_sequence = metadata.get("tool_sequence") or []
            candidates.append(
                {
                    "memory_id": mem["id"],
                    "text": mem["text"],
                    "tool_sequence": list(tool_sequence),
                    "confidence": mem.get("confidence"),
                    "tags": mem.get("tags", []),
                    "metadata": metadata,
                    "created_at": mem.get("created_at"),
                }
            )
            if len(candidates) >= limit:
                break
        return candidates

    def promote_playbook(
        self,
        memory_id: str,
        *,
        owner_confirmed: bool = False,
        routine_service=None,
    ) -> dict[str, Any]:
        """Promote a playbook-candidate memory into a trusted routine.

        Owner-gated: raises PermissionError unless owner_confirmed is True. A
        model can surface candidates but can never self-promote one to trusted.
        """
        if not owner_confirmed:
            raise PermissionError("playbook promotion requires owner confirmation")
        service = routine_service or self.routine_service
        if service is None:
            raise RuntimeError("routine service is not available for playbook promotion")

        memory = self.get(memory_id)
        if memory.get("kind") != "procedural" or "playbook_candidate" not in (
            memory.get("tags") or []
        ):
            raise ValueError("memory is not a playbook candidate")

        metadata = memory.get("metadata") or {}
        tool_sequence = list(metadata.get("tool_sequence") or [])
        if not tool_sequence:
            raise ValueError("playbook candidate has no tool_sequence to promote")

        # Import locally to avoid a hard module-level dependency cycle.
        from project_q.models import RoutineCreate

        steps = [
            {
                "step_type": "tool",
                "label": f"Step {index + 1}: {tool_id}",
                "tool_id": tool_id,
                "payload": {},
            }
            for index, tool_id in enumerate(tool_sequence)
        ]
        routine = service.create(
            RoutineCreate(
                name=f"Promoted playbook {memory_id}",
                goal=memory.get("text", "Promoted playbook"),
                description="Promoted from a playbook candidate by the owner.",
                status="active",
                trigger_type="manual",
                trusted=True,
                steps=steps,
                notes=f"Promoted from memory {memory_id}.",
            )
        )

        # Mark the source memory as owner-confirmed and tag it promoted.
        confirmed_tags = sorted(set((memory.get("tags") or []) + ["playbook_promoted"]))
        promoted_metadata = {**metadata, "promoted_routine_id": routine["id"]}
        self.update(
            memory_id,
            MemoryUpdate(
                owner_confirmed=True,
                tags=confirmed_tags,
                metadata=promoted_metadata,
            ),
        )
        return {
            "memory_id": memory_id,
            "routine_id": routine["id"],
            "trusted": bool(routine.get("trusted")),
            "tool_sequence": tool_sequence,
        }

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
        keys = set(row.keys())
        return {
            "id": row["id"],
            "text": row["text"],
            "kind": row["kind"],
            "source": row["source"],
            "confidence": row["confidence"],
            "owner_confirmed": bool(row["owner_confirmed"]),
            "tags": self.db.loads(row["tags_json"]),
            "metadata": self.db.loads(row["metadata_json"]),
            "source_type": row["source_type"] if "source_type" in keys else "owner",
            "trust_level": row["trust_level"] if "trust_level" in keys else "trusted",
            "inferred": bool(row["inferred"]) if "inferred" in keys else False,
            "evidence": (
                self.db.loads(row["evidence_json"])
                if "evidence_json" in keys and row["evidence_json"]
                else []
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_embedding(self, row: Any) -> list[float] | None:
        if "embedding_json" not in set(row.keys()):
            return None
        raw = row["embedding_json"]
        if not raw:
            return None
        try:
            vector = self.db.loads(raw)
        except Exception:  # noqa: BLE001
            return None
        if isinstance(vector, list) and vector:
            return [float(value) for value in vector]
        return None
