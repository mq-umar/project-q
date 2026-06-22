from __future__ import annotations

from typing import Any

from project_q.models import RoutineCreate, RoutineUpdate, utc_now
from project_q.storage import Database


class RoutineService:
    def __init__(self, db: Database, sync_service=None) -> None:
        self.db = db
        self.sync_service = sync_service
        self._ensure_version_schema()

    def _ensure_version_schema(self) -> None:
        with self.db.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS routine_versions (
                    id TEXT PRIMARY KEY,
                    routine_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    trigger_type TEXT NOT NULL,
                    trusted INTEGER NOT NULL DEFAULT 0,
                    tools_json TEXT NOT NULL DEFAULT '[]',
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    rollback_of_version INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(routine_id, version)
                );

                CREATE INDEX IF NOT EXISTS routine_versions_routine_version
                    ON routine_versions(routine_id, version);

                CREATE TRIGGER IF NOT EXISTS routine_versions_no_update
                BEFORE UPDATE ON routine_versions BEGIN
                    SELECT RAISE(ABORT, 'routine versions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS routine_versions_no_delete
                BEFORE DELETE ON routine_versions BEGIN
                    SELECT RAISE(ABORT, 'routine versions are immutable');
                END;
                """
            )

            routine_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(routines)").fetchall()
            }
            routine_additions = {
                "current_version": "INTEGER NOT NULL DEFAULT 1",
                "current_version_id": "TEXT",
            }
            for name, declaration in routine_additions.items():
                if name not in routine_columns:
                    conn.execute(f"ALTER TABLE routines ADD COLUMN {name} {declaration}")

            run_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(routine_runs)").fetchall()
            }
            run_additions = {
                "routine_version": "INTEGER",
                "routine_version_id": "TEXT",
                "routine_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, declaration in run_additions.items():
                if name not in run_columns:
                    conn.execute(f"ALTER TABLE routine_runs ADD COLUMN {name} {declaration}")

            rows = conn.execute("SELECT * FROM routines").fetchall()
            for row in rows:
                existing = conn.execute(
                    """
                    SELECT id, version
                    FROM routine_versions
                    WHERE routine_id = ?
                    ORDER BY version DESC
                    LIMIT 1
                    """,
                    (row["id"],),
                ).fetchone()
                if existing is None:
                    created_at = row["created_at"]
                    version_id = self._insert_version(
                        conn,
                        routine_id=row["id"],
                        version=1,
                        record=self._definition_payload_from_row(row),
                        created_at=created_at,
                    )
                    conn.execute(
                        """
                        UPDATE routines
                        SET current_version = 1, current_version_id = ?
                        WHERE id = ?
                        """,
                        (version_id, row["id"]),
                    )
                elif not row["current_version_id"]:
                    conn.execute(
                        """
                        UPDATE routines
                        SET current_version = ?, current_version_id = ?
                        WHERE id = ?
                        """,
                        (existing["version"], existing["id"], row["id"]),
                    )

            run_rows = conn.execute(
                """
                SELECT rr.id AS run_id, rr.routine_id, r.current_version, r.current_version_id,
                       rv.name, rv.goal, rv.description, rv.trigger_type, rv.trusted,
                       rv.tools_json, rv.steps_json, rv.notes
                FROM routine_runs rr
                JOIN routines r ON r.id = rr.routine_id
                LEFT JOIN routine_versions rv ON rv.id = r.current_version_id
                WHERE rr.routine_version IS NULL
                   OR rr.routine_version_id IS NULL
                   OR rr.routine_version_id = ''
                   OR rr.routine_snapshot_json = ''
                   OR rr.routine_snapshot_json = '{}'
                """
            ).fetchall()
            for row in run_rows:
                snapshot = self._snapshot_from_joined_version_row(row)
                conn.execute(
                    """
                    UPDATE routine_runs
                    SET routine_version = ?, routine_version_id = ?, routine_snapshot_json = ?
                    WHERE id = ?
                    """,
                    (
                        row["current_version"],
                        row["current_version_id"],
                        self.db.dumps(snapshot),
                        row["run_id"],
                    ),
                )

    def create(self, payload: RoutineCreate) -> dict[str, Any]:
        routine_id = self.db.make_id("routine")
        version_id = self.db.make_id("routinever")
        now = utc_now()
        record = payload.model_dump()
        tools = self._normalize_tools(record["tools"], record["steps"])
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO routines (
                    id, name, goal, description, status, trigger_type,
                    trusted, tools_json, steps_json, notes, current_version,
                    current_version_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
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
                    version_id,
                    now,
                    now,
                ),
            )
            self._insert_version(
                conn,
                routine_id=routine_id,
                version=1,
                version_id=version_id,
                record=self._definition_payload(record),
                created_at=now,
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
        current_definition = self._definition_payload(current)
        updated_definition = self._definition_payload(updated)
        definition_changed = current_definition != updated_definition
        version = int(current.get("version") or 1)
        version_id = current.get("version_id")
        now = utc_now()
        with self.db.connection() as conn:
            if definition_changed:
                version += 1
                version_id = self._insert_version(
                    conn,
                    routine_id=routine_id,
                    version=version,
                    record=updated_definition,
                    created_at=now,
                )
            conn.execute(
                """
                UPDATE routines
                SET name = ?, goal = ?, description = ?, status = ?, trigger_type = ?,
                    trusted = ?, tools_json = ?, steps_json = ?, notes = ?,
                    current_version = ?, current_version_id = ?, updated_at = ?
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
                    version,
                    version_id,
                    now,
                    routine_id,
                ),
            )
        record = self.get(routine_id)
        self._emit("updated", record)
        return record

    def list_versions(self, routine_id: str) -> list[dict[str, Any]]:
        self.get(routine_id)
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM routine_versions
                WHERE routine_id = ?
                ORDER BY version DESC
                """,
                (routine_id,),
            ).fetchall()
        return [self._version_row_to_dict(row) for row in rows]

    def get_version(self, routine_id: str, version: int) -> dict[str, Any]:
        self.get(routine_id)
        with self.db.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM routine_versions
                WHERE routine_id = ? AND version = ?
                """,
                (routine_id, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"{routine_id}@{version}")
        return self._version_row_to_dict(row)

    def rollback(self, routine_id: str, *, version: int, owner_confirmed: bool) -> dict[str, Any]:
        if not owner_confirmed:
            raise PermissionError("routine rollback requires explicit owner confirmation")
        current = self.get(routine_id)
        target = self.get_version(routine_id, version)
        next_version = int(current.get("version") or 1) + 1
        now = utc_now()
        version_id = self.db.make_id("routinever")
        target_definition = self._definition_payload(target)
        with self.db.connection() as conn:
            self._insert_version(
                conn,
                routine_id=routine_id,
                version=next_version,
                version_id=version_id,
                record=target_definition,
                created_at=now,
                rollback_of_version=version,
            )
            conn.execute(
                """
                UPDATE routines
                SET name = ?, goal = ?, description = ?, status = 'active',
                    trigger_type = ?, trusted = ?, tools_json = ?, steps_json = ?,
                    notes = ?, current_version = ?, current_version_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    target_definition["name"],
                    target_definition["goal"],
                    target_definition["description"],
                    target_definition["trigger_type"],
                    1 if target_definition["trusted"] else 0,
                    self.db.dumps(target_definition["tools"]),
                    self.db.dumps(target_definition["steps"]),
                    target_definition["notes"],
                    next_version,
                    version_id,
                    now,
                    routine_id,
                ),
            )
        record = self.get(routine_id)
        self._emit("rollback", record)
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

    def _definition_payload(self, record: dict[str, Any]) -> dict[str, Any]:
        steps = list(record.get("steps") or [])
        tools = self._normalize_tools(list(record.get("tools") or []), steps)
        return {
            "name": str(record["name"]),
            "goal": str(record["goal"]),
            "description": str(record.get("description") or ""),
            "trigger_type": str(record.get("trigger_type") or "manual"),
            "trusted": bool(record.get("trusted", False)),
            "tools": tools,
            "steps": steps,
            "notes": str(record.get("notes") or ""),
        }

    def _definition_payload_from_row(self, row: Any) -> dict[str, Any]:
        return self._definition_payload(
            {
                "name": row["name"],
                "goal": row["goal"],
                "description": row["description"],
                "trigger_type": row["trigger_type"],
                "trusted": bool(row["trusted"]),
                "tools": self.db.loads(row["tools_json"]),
                "steps": self.db.loads(row["steps_json"]),
                "notes": row["notes"],
            }
        )

    def _insert_version(
        self,
        conn: Any,
        *,
        routine_id: str,
        version: int,
        record: dict[str, Any],
        created_at: str,
        version_id: str | None = None,
        rollback_of_version: int | None = None,
    ) -> str:
        version_id = version_id or self.db.make_id("routinever")
        definition = self._definition_payload(record)
        conn.execute(
            """
            INSERT INTO routine_versions (
                id, routine_id, version, name, goal, description, trigger_type,
                trusted, tools_json, steps_json, notes, rollback_of_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                routine_id,
                version,
                definition["name"],
                definition["goal"],
                definition["description"],
                definition["trigger_type"],
                1 if definition["trusted"] else 0,
                self.db.dumps(definition["tools"]),
                self.db.dumps(definition["steps"]),
                definition["notes"],
                rollback_of_version,
                created_at,
            ),
        )
        return version_id

    def _version_row_to_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["routine_id"],
            "routine_id": row["routine_id"],
            "version": row["version"],
            "version_id": row["id"],
            "name": row["name"],
            "goal": row["goal"],
            "description": row["description"],
            "trigger_type": row["trigger_type"],
            "trusted": bool(row["trusted"]),
            "tools": self.db.loads(row["tools_json"]),
            "steps": self.db.loads(row["steps_json"]),
            "notes": row["notes"],
            "rollback_of_version": row["rollback_of_version"],
            "created_at": row["created_at"],
        }

    def _snapshot_from_joined_version_row(self, row: Any) -> dict[str, Any]:
        if row["name"] is None:
            return {}
        return {
            "id": row["routine_id"],
            "version": row["current_version"],
            "version_id": row["current_version_id"],
            "name": row["name"],
            "goal": row["goal"],
            "description": row["description"],
            "trigger_type": row["trigger_type"],
            "trusted": bool(row["trusted"]),
            "tools": self.db.loads(row["tools_json"]),
            "steps": self.db.loads(row["steps_json"]),
            "notes": row["notes"],
        }

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        keys = row.keys()
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
            "version": row["current_version"] if "current_version" in keys else 1,
            "version_id": row["current_version_id"] if "current_version_id" in keys else None,
            "last_run_at": row["last_run_at"] if "last_run_at" in keys else None,
            "last_run_outcome": row["last_run_outcome"] if "last_run_outcome" in keys else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
