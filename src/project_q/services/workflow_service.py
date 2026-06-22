from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from project_q.models import utc_now
from project_q.services.sync import SyncEventService
from project_q.services.workflow_graph import WorkflowGraph


_RUN_TERMINAL = {"completed", "failed", "cancelled"}
_NODE_TERMINAL = {"succeeded", "failed", "skipped", "cancelled"}


class WorkflowStateConflict(ValueError):
    """Raised when a valid workflow request cannot apply in the current state."""


class WorkflowService:
    def __init__(self, db, sync_service=None) -> None:
        self.db = db
        self.sync_service = sync_service
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.db.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_definitions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    current_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_definition_versions (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL REFERENCES workflow_definitions(id),
                    version INTEGER NOT NULL,
                    definition_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(workflow_id, version)
                );

                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL REFERENCES workflow_definitions(id),
                    workflow_version INTEGER NOT NULL,
                    workflow_version_id TEXT NOT NULL,
                    definition_snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner_approved INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    cancel_reason TEXT NOT NULL DEFAULT '',
                    retry_of_run_id TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_node_runs (
                    id TEXT PRIMARY KEY,
                    workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
                    node_key TEXT NOT NULL,
                    node_index INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    node_snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    process_id INTEGER,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(workflow_run_id, node_key, attempt)
                );

                CREATE TABLE IF NOT EXISTS workflow_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
                    node_run_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
                    node_run_id TEXT,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS workflow_runs_status_updated
                    ON workflow_runs(status, updated_at);
                CREATE INDEX IF NOT EXISTS workflow_node_runs_run_status
                    ON workflow_node_runs(workflow_run_id, status);
                CREATE INDEX IF NOT EXISTS workflow_events_run_sequence
                    ON workflow_events(workflow_run_id, sequence);
                CREATE INDEX IF NOT EXISTS workflow_checkpoints_run_sequence
                    ON workflow_checkpoints(workflow_run_id, sequence);

                CREATE TRIGGER IF NOT EXISTS workflow_definition_versions_no_update
                BEFORE UPDATE ON workflow_definition_versions BEGIN
                    SELECT RAISE(ABORT, 'workflow definition versions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS workflow_definition_versions_no_delete
                BEFORE DELETE ON workflow_definition_versions BEGIN
                    SELECT RAISE(ABORT, 'workflow definition versions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS workflow_events_no_update
                BEFORE UPDATE ON workflow_events BEGIN
                    SELECT RAISE(ABORT, 'workflow events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS workflow_events_no_delete
                BEFORE DELETE ON workflow_events BEGIN
                    SELECT RAISE(ABORT, 'workflow events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS workflow_checkpoints_no_update
                BEFORE UPDATE ON workflow_checkpoints BEGIN
                    SELECT RAISE(ABORT, 'workflow checkpoints are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS workflow_checkpoints_no_delete
                BEFORE DELETE ON workflow_checkpoints BEGIN
                    SELECT RAISE(ABORT, 'workflow checkpoints are append-only');
                END;
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(workflow_node_runs)").fetchall()
            }
            if "node_index" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_node_runs ADD COLUMN node_index INTEGER NOT NULL DEFAULT 0"
                )

    def create_definition(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        definition = self._validated_definition(payload)
        workflow_id = self.db.make_id("workflow")
        version_id = self.db.make_id("workflowver")
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_definitions (
                    id, name, description, status, current_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    workflow_id,
                    definition["name"],
                    definition["description"],
                    definition["status"],
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO workflow_definition_versions (
                    id, workflow_id, version, definition_json, created_at
                ) VALUES (?, ?, 1, ?, ?)
                """,
                (version_id, workflow_id, self.db.dumps(definition), now),
            )
        created = self.get_definition(workflow_id)
        self._emit_definition("created", created)
        return created

    def update_definition(
        self,
        workflow_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        current = self.get_definition(workflow_id)
        definition = self._validated_definition(payload)
        version = int(current["version"]) + 1
        version_id = self.db.make_id("workflowver")
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_definition_versions (
                    id, workflow_id, version, definition_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    workflow_id,
                    version,
                    self.db.dumps(definition),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE workflow_definitions
                SET name = ?, description = ?, status = ?,
                    current_version = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    definition["name"],
                    definition["description"],
                    definition["status"],
                    version,
                    now,
                    workflow_id,
                ),
            )
        updated = self.get_definition(workflow_id)
        self._emit_definition("updated", updated)
        return updated

    def list_definitions(self, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT d.*, v.id AS version_id, v.definition_json, v.created_at AS version_created_at
                FROM workflow_definitions d
                JOIN workflow_definition_versions v
                  ON v.workflow_id = d.id AND v.version = d.current_version
                ORDER BY d.updated_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [self._definition_row(row) for row in rows]

    def get_definition(self, workflow_id: str) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute(
                """
                SELECT d.*, v.id AS version_id, v.definition_json, v.created_at AS version_created_at
                FROM workflow_definitions d
                JOIN workflow_definition_versions v
                  ON v.workflow_id = d.id AND v.version = d.current_version
                WHERE d.id = ?
                """,
                (workflow_id,),
            ).fetchone()
        if row is None:
            raise KeyError(workflow_id)
        return self._definition_row(row)

    def list_definition_versions(self, workflow_id: str) -> list[dict[str, Any]]:
        self.get_definition(workflow_id)
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT id AS version_id, workflow_id, version, definition_json, created_at
                FROM workflow_definition_versions
                WHERE workflow_id = ?
                ORDER BY version DESC
                """,
                (workflow_id,),
            ).fetchall()
        return [
            {
                **self.db.loads(row["definition_json"]),
                "id": row["workflow_id"],
                "version": row["version"],
                "version_id": row["version_id"],
                "version_created_at": row["created_at"],
            }
            for row in rows
        ]

    def delete_definition(self, workflow_id: str) -> dict[str, Any]:
        current = self.get_definition(workflow_id)
        if current["status"] == "archived":
            return current
        payload = {key: current[key] for key in ("name", "description", "parallelism", "nodes")}
        payload["status"] = "archived"
        return self.update_definition(workflow_id, payload)

    def start_run(
        self,
        workflow_id: str,
        *,
        owner_approved: bool,
        retry_of_run_id: str | None = None,
    ) -> dict[str, Any]:
        definition = self.get_definition(workflow_id)
        previous = None
        if retry_of_run_id is not None:
            previous = self.get_run(retry_of_run_id, include_events=False)
            if previous["workflow_id"] != workflow_id:
                raise ValueError("retry run belongs to a different workflow")
            if previous["status"] not in _RUN_TERMINAL:
                raise WorkflowStateConflict("only terminal workflow runs can be retried")
            definition = dict(previous["definition_snapshot"])
        elif definition["status"] != "active":
            raise WorkflowStateConflict("archived workflows cannot be started")
        run_id = self.db.make_id("workflowrun")
        now = utc_now()
        snapshot = {
            key: definition[key]
            for key in (
                "id",
                "version",
                "version_id",
                "name",
                "description",
                "status",
                "parallelism",
                "nodes",
            )
        }
        previous_nodes = {
            node["node_key"]: node
            for node in (previous["nodes"] if previous is not None else [])
        }
        reused_nodes: list[tuple[str, str]] = []
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs (
                    id, workflow_id, workflow_version, workflow_version_id,
                    definition_snapshot_json, status, owner_approved,
                    retry_of_run_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    workflow_id,
                    definition["version"],
                    definition["version_id"],
                    self.db.dumps(snapshot),
                    1 if owner_approved else 0,
                    retry_of_run_id,
                    now,
                    now,
                ),
            )
            for node_index, node in enumerate(definition["nodes"]):
                previous_node = previous_nodes.get(node["key"])
                reused = (
                    previous_node is not None
                    and previous_node["status"] == "succeeded"
                    and node.get("kind") != "approval"
                )
                node_run_id = self.db.make_id("workflownode")
                status = "succeeded" if reused else "pending"
                result = previous_node["result"] if reused else {}
                attempt = int(previous_node["attempt"]) + 1 if previous_node else 1
                conn.execute(
                    """
                    INSERT INTO workflow_node_runs (
                        id, workflow_run_id, node_key, node_index, attempt, node_snapshot_json,
                        status, result_json, created_at, started_at, completed_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_run_id,
                        run_id,
                        node["key"],
                        node_index,
                        attempt,
                        self.db.dumps(node),
                        status,
                        self.db.dumps(result),
                        now,
                        now if reused else None,
                        now if reused else None,
                        now,
                    ),
                )
                if reused:
                    reused_nodes.append((node_run_id, node["key"]))
            self._insert_event(
                conn,
                run_id,
                event_type="workflow_run_created",
                payload={
                    "workflow_id": workflow_id,
                    "workflow_version": definition["version"],
                    "retry_of_run_id": retry_of_run_id or "",
                },
            )
            for node_run_id, node_key in reused_nodes:
                self._insert_event(
                    conn,
                    run_id,
                    node_run_id=node_run_id,
                    event_type="node_reused",
                    payload={
                        "node_key": node_key,
                        "retry_of_run_id": retry_of_run_id or "",
                    },
                )
                self._insert_checkpoint(
                    conn,
                    run_id,
                    node_run_id=node_run_id,
                    state=self._node_run_from_conn(conn, node_run_id),
                )
            self._insert_checkpoint(conn, run_id, state={"status": "queued"})
        return self.get_run(run_id)

    def list_runs(
        self,
        *,
        workflow_id: str | None = None,
        statuses: tuple[str, ...] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if workflow_id:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self.db.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM workflow_runs
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            run_ids = [row["id"] for row in rows]
            node_rows = []
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                node_rows = conn.execute(
                    f"""
                    SELECT *
                    FROM workflow_node_runs
                    WHERE workflow_run_id IN ({placeholders})
                    ORDER BY workflow_run_id, node_index, attempt
                    """,
                    tuple(run_ids),
                ).fetchall()
        nodes_by_run: dict[str, list[dict[str, Any]]] = {run_id: [] for run_id in run_ids}
        for node_row in node_rows:
            nodes_by_run[node_row["workflow_run_id"]].append(self._node_row(node_row))
        records = [self._run_row(row) for row in rows]
        for record in records:
            record["nodes"] = nodes_by_run[record["id"]]
        return records

    def get_run(self, run_id: str, *, include_events: bool = True) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            node_rows = conn.execute(
                """
                SELECT *
                FROM workflow_node_runs
                WHERE workflow_run_id = ?
                ORDER BY node_index, attempt
                """,
                (run_id,),
            ).fetchall()
        if row is None:
            raise KeyError(run_id)
        record = self._run_row(row)
        record["nodes"] = [self._node_row(item) for item in node_rows]
        if include_events:
            record["events"] = self.list_events(run_id)
        return record

    def get_node_run(self, node_run_id: str) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_node_runs WHERE id = ?",
                (node_run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(node_run_id)
        return self._node_row(row)

    def _node_run_from_conn(self, conn: Any, node_run_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM workflow_node_runs WHERE id = ?",
            (node_run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(node_run_id)
        return self._node_row(row)

    def mark_node_running(self, node_run_id: str, *, process_id: int | None) -> dict[str, Any]:
        node = self.get_node_run(node_run_id)
        if node["status"] != "pending":
            raise WorkflowStateConflict(f"node cannot start from {node['status']}")
        now = utc_now()
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE workflow_node_runs
                SET status = 'running', process_id = ?, started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (process_id, now, now, node_run_id),
            )
            if cursor.rowcount != 1:
                raise WorkflowStateConflict("node was claimed by another worker")
            conn.execute(
                """
                UPDATE workflow_runs
                SET status = CASE WHEN status = 'queued' THEN 'running' ELSE status END,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, node["workflow_run_id"]),
            )
            current = self._node_run_from_conn(conn, node_run_id)
            self._insert_event(
                conn,
                node["workflow_run_id"],
                node_run_id=node_run_id,
                event_type="node_started",
                payload={"node_key": node["node_key"], "process_id": process_id},
            )
            self._insert_checkpoint(
                conn,
                node["workflow_run_id"],
                node_run_id=node_run_id,
                state=current,
            )
        return current

    def mark_node_waiting_approval(self, node_run_id: str) -> dict[str, Any]:
        node = self.get_node_run(node_run_id)
        if node["status"] != "pending":
            raise WorkflowStateConflict(f"approval node cannot wait from {node['status']}")
        if node["node_snapshot"].get("kind") != "approval":
            raise ValueError("only approval nodes can wait for an owner decision")
        now = utc_now()
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE workflow_node_runs
                SET status = 'waiting_approval', started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, now, node_run_id),
            )
            if cursor.rowcount != 1:
                raise WorkflowStateConflict("approval node was claimed by another coordinator")
            conn.execute(
                """
                UPDATE workflow_runs
                SET status = CASE WHEN status = 'queued' THEN 'running' ELSE status END,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (now, now, node["workflow_run_id"]),
            )
            current = self._node_run_from_conn(conn, node_run_id)
            self._insert_event(
                conn,
                node["workflow_run_id"],
                node_run_id=node_run_id,
                event_type="approval_requested",
                payload={
                    "node_key": node["node_key"],
                    "prompt": node["node_snapshot"].get("prompt", "Owner decision required"),
                },
            )
            self._insert_checkpoint(
                conn,
                node["workflow_run_id"],
                node_run_id=node_run_id,
                state=current,
            )
        return current

    def decide_approval(
        self,
        node_run_id: str,
        *,
        approved: bool,
        note: str = "",
    ) -> dict[str, Any]:
        if not isinstance(approved, bool):
            raise ValueError("approved must be a boolean")
        node = self.get_node_run(node_run_id)
        if node["node_snapshot"].get("kind") != "approval":
            raise ValueError("node is not an approval checkpoint")
        clean_note = str(note or "").strip()[:1000]
        status = "succeeded" if approved else "failed"
        error = "" if approved else (clean_note or "owner rejected the approval checkpoint")
        result = {"approved": approved, "note": clean_note}
        now = utc_now()
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE workflow_node_runs
                SET status = ?, result_json = ?, error = ?, process_id = NULL,
                    completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'waiting_approval'
                """,
                (
                    status,
                    self.db.dumps(result),
                    error,
                    now,
                    now,
                    node_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkflowStateConflict("approval checkpoint is not waiting for a decision")
            conn.execute(
                "UPDATE workflow_runs SET updated_at = ? WHERE id = ?",
                (now, node["workflow_run_id"]),
            )
            current = self._node_run_from_conn(conn, node_run_id)
            self._insert_event(
                conn,
                node["workflow_run_id"],
                node_run_id=node_run_id,
                event_type="approval_decided",
                payload={"node_key": node["node_key"], **result},
            )
            self._insert_event(
                conn,
                node["workflow_run_id"],
                node_run_id=node_run_id,
                event_type=f"node_{status}",
                payload={"node_key": node["node_key"], "result": result, "error": error},
            )
            self._insert_checkpoint(
                conn,
                node["workflow_run_id"],
                node_run_id=node_run_id,
                state=current,
            )
        return current

    def mark_node_terminal(
        self,
        node_run_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        clean_status = str(status).strip().lower()
        if clean_status not in _NODE_TERMINAL:
            raise ValueError("invalid terminal node status")
        node = self.get_node_run(node_run_id)
        if node["status"] in _NODE_TERMINAL:
            return node
        if node["status"] not in {"pending", "running", "waiting_approval"}:
            raise WorkflowStateConflict(f"node cannot complete from {node['status']}")
        now = utc_now()
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE workflow_node_runs
                SET status = ?, result_json = ?, error = ?, process_id = NULL,
                    completed_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'running', 'waiting_approval')
                """,
                (
                    clean_status,
                    self.db.dumps(dict(result or {})),
                    str(error or "")[:4000],
                    now,
                    now,
                    node_run_id,
                ),
            )
            if cursor.rowcount != 1:
                current = conn.execute(
                    "SELECT status FROM workflow_node_runs WHERE id = ?",
                    (node_run_id,),
                ).fetchone()
                if current is not None and current["status"] in _NODE_TERMINAL:
                    return self.get_node_run(node_run_id)
                raise WorkflowStateConflict("node terminal transition lost a concurrent race")
            conn.execute(
                "UPDATE workflow_runs SET updated_at = ? WHERE id = ?",
                (now, node["workflow_run_id"]),
            )
            current = self._node_run_from_conn(conn, node_run_id)
            self._insert_event(
                conn,
                node["workflow_run_id"],
                node_run_id=node_run_id,
                event_type=f"node_{clean_status}",
                payload={
                    "node_key": node["node_key"],
                    "result": dict(result or {}),
                    "error": str(error or "")[:1000],
                },
            )
            self._insert_checkpoint(
                conn,
                node["workflow_run_id"],
                node_run_id=node_run_id,
                state=current,
            )
        return current

    def set_node_process_id(self, node_run_id: str, process_id: int) -> dict[str, Any]:
        node = self.get_node_run(node_run_id)
        if node["status"] != "running":
            raise WorkflowStateConflict("process id can only be assigned to a running node")
        if int(process_id) <= 0:
            raise ValueError("process_id must be positive")
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE workflow_node_runs
                SET process_id = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (int(process_id), utc_now(), node_run_id),
            )
            if cursor.rowcount != 1:
                raise WorkflowStateConflict("process id assignment lost a concurrent race")
        return self.get_node_run(node_run_id)

    def set_run_terminal(
        self,
        run_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        clean_status = str(status).strip().lower()
        if clean_status not in _RUN_TERMINAL:
            raise ValueError("invalid terminal workflow status")
        run = self.get_run(run_id, include_events=False)
        if run["status"] in _RUN_TERMINAL:
            return run
        if run["status"] == "cancelling" and clean_status != "cancelled":
            raise WorkflowStateConflict("cancelling workflow runs can only become cancelled")
        now = utc_now()
        allowed_previous = ("queued", "running", "cancelling") if clean_status == "cancelled" else ("queued", "running")
        placeholders = ",".join("?" for _ in allowed_previous)
        with self.db.connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE workflow_runs
                SET status = ?, result_json = ?, error = ?,
                    completed_at = ?, updated_at = ?
                WHERE id = ? AND status IN ({placeholders})
                """,
                (
                    clean_status,
                    self.db.dumps(dict(result or {})),
                    str(error or "")[:4000],
                    now,
                    now,
                    run_id,
                    *allowed_previous,
                ),
            )
            if cursor.rowcount != 1:
                current = conn.execute(
                    "SELECT status FROM workflow_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if current is not None and current["status"] in _RUN_TERMINAL:
                    return self.get_run(run_id, include_events=False)
                raise WorkflowStateConflict("workflow terminal transition lost a concurrent race")
            self._insert_event(
                conn,
                run_id,
                event_type=f"workflow_{clean_status}",
                payload={"result": dict(result or {}), "error": str(error or "")[:1000]},
            )
            self._insert_checkpoint(
                conn,
                run_id,
                state={"status": clean_status, "result": dict(result or {})},
            )
        return self.get_run(run_id)

    def request_cancel(self, run_id: str, *, reason: str = "") -> dict[str, Any]:
        run = self.get_run(run_id, include_events=False)
        if run["status"] in _RUN_TERMINAL or run["status"] == "cancelling":
            return run
        now = utc_now()
        clean_reason = str(reason or "").strip()[:500] or "Cancellation requested"
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE workflow_runs
                SET status = 'cancelling', cancel_requested = 1,
                    cancel_reason = ?, updated_at = ?
                WHERE id = ? AND status NOT IN ('completed', 'failed', 'cancelled', 'cancelling')
                """,
                (clean_reason, now, run_id),
            )
            if cursor.rowcount != 1:
                return self.get_run(run_id, include_events=False)
            self._insert_event(
                conn,
                run_id,
                event_type="workflow_cancellation_requested",
                payload={"reason": clean_reason},
            )
            self._insert_checkpoint(
                conn,
                run_id,
                state={"status": "cancelling", "reason": clean_reason},
            )
        return self.get_run(run_id, include_events=False)

    def prune_history(self, *, retention_days: int | None = None) -> dict[str, Any]:
        """Bound append-only event/checkpoint growth by discarding the trail of
        terminal runs older than the retention window (run records are retained)."""
        days = 30 if retention_days is None else int(retention_days)
        if days <= 0:
            return {"pruned_events": 0, "pruned_checkpoints": 0, "pruned_runs": 0, "skipped": "disabled"}
        from datetime import UTC, datetime, timedelta

        cutoff = (
            (datetime.now(UTC) - timedelta(days=days))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        with self.db.connection() as conn:
            old_runs = [
                row["id"]
                for row in conn.execute(
                    """
                    SELECT id FROM workflow_runs
                    WHERE status IN ('completed', 'failed', 'cancelled')
                      AND COALESCE(completed_at, updated_at) < ?
                    """,
                    (cutoff,),
                ).fetchall()
            ]
            pruned_events = 0
            pruned_checkpoints = 0
            for run_id in old_runs:
                pruned_events += conn.execute(
                    "DELETE FROM workflow_events WHERE workflow_run_id = ?", (run_id,)
                ).rowcount
                pruned_checkpoints += conn.execute(
                    "DELETE FROM workflow_checkpoints WHERE workflow_run_id = ?", (run_id,)
                ).rowcount
        return {
            "pruned_events": pruned_events,
            "pruned_checkpoints": pruned_checkpoints,
            "pruned_runs": len(old_runs),
            "cutoff": cutoff,
            "retention_days": days,
        }

    def append_event(
        self,
        run_id: str,
        *,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        node_run_id: str | None = None,
    ) -> dict[str, Any]:
        with self.db.connection() as conn:
            return self._insert_event(
                conn,
                run_id,
                event_type=event_type,
                payload=payload,
                node_run_id=node_run_id,
            )

    def _insert_event(
        self,
        conn: Any,
        run_id: str,
        *,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        node_run_id: str | None = None,
    ) -> dict[str, Any]:
        event_id = self.db.make_id("workflowevent")
        created_at = utc_now()
        clean_payload = SyncEventService._redact(dict(payload or {}))
        cursor = conn.execute(
            """
            INSERT INTO workflow_events (
                id, workflow_run_id, node_run_id, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                run_id,
                node_run_id,
                str(event_type or "").strip()[:120],
                self.db.dumps(clean_payload),
                created_at,
            ),
        )
        sequence = int(cursor.lastrowid)
        return {
            "id": event_id,
            "sequence": sequence,
            "workflow_run_id": run_id,
            "node_run_id": node_run_id,
            "event_type": str(event_type or "").strip()[:120],
            "payload": clean_payload,
            "created_at": created_at,
        }

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM workflow_events
                WHERE workflow_run_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (run_id, max(0, int(after_sequence)), max(1, min(int(limit), 1000))),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "sequence": row["sequence"],
                "workflow_run_id": row["workflow_run_id"],
                "node_run_id": row["node_run_id"],
                "event_type": row["event_type"],
                "payload": self.db.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def append_checkpoint(
        self,
        run_id: str,
        *,
        state: Mapping[str, Any],
        node_run_id: str | None = None,
    ) -> dict[str, Any]:
        with self.db.connection() as conn:
            return self._insert_checkpoint(
                conn,
                run_id,
                state=state,
                node_run_id=node_run_id,
            )

    def _insert_checkpoint(
        self,
        conn: Any,
        run_id: str,
        *,
        state: Mapping[str, Any],
        node_run_id: str | None = None,
    ) -> dict[str, Any]:
        checkpoint_id = self.db.make_id("workflowcheckpoint")
        created_at = utc_now()
        clean_state = SyncEventService._redact(dict(state))
        cursor = conn.execute(
            """
            INSERT INTO workflow_checkpoints (
                id, workflow_run_id, node_run_id, state_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                checkpoint_id,
                run_id,
                node_run_id,
                self.db.dumps(clean_state),
                created_at,
            ),
        )
        sequence = int(cursor.lastrowid)
        return {
            "id": checkpoint_id,
            "sequence": sequence,
            "workflow_run_id": run_id,
            "node_run_id": node_run_id,
            "state": clean_state,
            "created_at": created_at,
        }

    def list_checkpoints(self, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM workflow_checkpoints
                WHERE workflow_run_id = ?
                ORDER BY sequence
                LIMIT ?
                """,
                (run_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "sequence": row["sequence"],
                "workflow_run_id": row["workflow_run_id"],
                "node_run_id": row["node_run_id"],
                "state": self.db.loads(row["state_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _validated_definition(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TypeError("workflow definition must be an object")
        name = str(payload.get("name", "")).strip()
        if not name or len(name) > 200:
            raise ValueError("workflow name must contain 1 to 200 characters")
        description = str(payload.get("description", "")).strip()[:4000]
        status = str(payload.get("status", "active")).strip().lower()
        if status not in {"active", "archived"}:
            raise ValueError("workflow status must be active or archived")
        raw_nodes = payload.get("nodes")
        if not isinstance(raw_nodes, list):
            raise ValueError("workflow nodes must be a list")
        graph = WorkflowGraph(raw_nodes, parallelism=payload.get("parallelism", 1))
        source_by_key = {str(item.get("key", "")).strip(): item for item in raw_nodes}
        nodes: list[dict[str, Any]] = []
        for graph_node in graph:
            source = source_by_key[graph_node.key]
            kind = str(source.get("kind", "")).strip().lower()
            if kind not in {"agent", "tool", "delay", "approval"}:
                raise ValueError(f"node {graph_node.key!r} has invalid kind")
            node = {
                **graph_node.to_dict(),
                "kind": kind,
                "label": str(source.get("label", graph_node.key)).strip()[:200] or graph_node.key,
                "payload": self._mapping(source.get("payload", {}), "node payload"),
                "timeout_seconds": self._bounded_int(
                    source.get("timeout_seconds", 1800),
                    "timeout_seconds",
                    minimum=1,
                    maximum=86400,
                ),
                "requires_owner_approval": bool(source.get("requires_owner_approval", False)),
            }
            if kind == "agent":
                agent_id = str(source.get("agent_id", "")).strip()
                if not agent_id or len(agent_id) > 200:
                    raise ValueError(f"agent node {graph_node.key!r} requires agent_id")
                node["agent_id"] = agent_id
            elif kind == "tool":
                tool_id = str(source.get("tool_id", "")).strip()
                if not tool_id or len(tool_id) > 200:
                    raise ValueError(f"tool node {graph_node.key!r} requires tool_id")
                node["tool_id"] = tool_id
            elif kind == "delay":
                node["delay_seconds"] = self._bounded_int(
                    source.get("delay_seconds"),
                    "delay_seconds",
                    minimum=1,
                    maximum=3600,
                )
            else:
                prompt = str(source.get("prompt", "")).strip()
                if not prompt or len(prompt) > 1000:
                    raise ValueError(
                        f"approval node {graph_node.key!r} requires a prompt of 1 to 1000 characters"
                    )
                node["prompt"] = prompt
            nodes.append(node)
        return {
            "name": name,
            "description": description,
            "status": status,
            "parallelism": graph.parallelism,
            "nodes": nodes,
        }

    @staticmethod
    def _mapping(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{label} must be an object")
        return dict(value)

    @staticmethod
    def _bounded_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{label} must be an integer")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be an integer") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"{label} must be between {minimum} and {maximum}")
        return number

    def _definition_row(self, row: Any) -> dict[str, Any]:
        definition = self.db.loads(row["definition_json"])
        return {
            **definition,
            "id": row["id"],
            "version": row["current_version"],
            "version_id": row["version_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "version_created_at": row["version_created_at"],
        }

    def _run_row(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "workflow_id": row["workflow_id"],
            "workflow_version": row["workflow_version"],
            "workflow_version_id": row["workflow_version_id"],
            "definition_snapshot": self.db.loads(row["definition_snapshot_json"]),
            "status": row["status"],
            "owner_approved": bool(row["owner_approved"]),
            "cancel_requested": bool(row["cancel_requested"]),
            "cancel_reason": row["cancel_reason"] or "",
            "retry_of_run_id": row["retry_of_run_id"],
            "result": self.db.loads(row["result_json"]),
            "error": row["error"] or "",
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "updated_at": row["updated_at"],
        }

    def _node_row(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "workflow_run_id": row["workflow_run_id"],
            "node_key": row["node_key"],
            "node_index": row["node_index"],
            "attempt": row["attempt"],
            "node_snapshot": self.db.loads(row["node_snapshot_json"]),
            "status": row["status"],
            "process_id": row["process_id"],
            "result": self.db.loads(row["result_json"]),
            "error": row["error"] or "",
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "updated_at": row["updated_at"],
        }

    def _emit_definition(self, operation: str, payload: dict[str, Any]) -> None:
        if self.sync_service is not None:
            self.sync_service.append(
                resource_type="workflow",
                resource_id=payload["id"],
                operation=operation,
                payload=payload,
            )
