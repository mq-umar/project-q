from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._lock, self.connection() as conn:
            fts_existed = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
            ).fetchone() is not None
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'text',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    owner_confirmed INTEGER NOT NULL DEFAULT 1,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    priority INTEGER NOT NULL DEFAULT 3,
                    status TEXT NOT NULL DEFAULT 'pending',
                    due_at TEXT,
                    source TEXT NOT NULL DEFAULT 'owner',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    agent_type TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    tools_json TEXT NOT NULL DEFAULT '[]',
                    memory_scope TEXT NOT NULL,
                    time_budget_minutes INTEGER NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS routines (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    trusted INTEGER NOT NULL DEFAULT 0,
                    tools_json TEXT NOT NULL DEFAULT '[]',
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    reply TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    reasoning_mode TEXT NOT NULL,
                    model_name TEXT NOT NULL DEFAULT '',
                    created_task_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_memory_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_agent_ids_json TEXT NOT NULL DEFAULT '[]',
                    executed_tools_json TEXT NOT NULL DEFAULT '[]',
                    blocked_tools_json TEXT NOT NULL DEFAULT '[]',
                    warning TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS routine_runs (
                    id TEXT PRIMARY KEY,
                    routine_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    reply TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    step_results_json TEXT NOT NULL DEFAULT '[]',
                    blocked_steps_json TEXT NOT NULL DEFAULT '[]',
                    warning TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    action_tier INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT 'local-mvp',
                    input_sources_json TEXT NOT NULL DEFAULT '[]',
                    approved_by_owner INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS secrets (
                    name TEXT PRIMARY KEY,
                    encrypted_blob BLOB NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS learning_runs (
                    id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    findings_json TEXT NOT NULL DEFAULT '[]',
                    created_task_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_memory_ids_json TEXT NOT NULL DEFAULT '[]',
                    warning TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS diagnostic_runs (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    checks_json TEXT NOT NULL DEFAULT '[]',
                    findings_json TEXT NOT NULL DEFAULT '[]',
                    created_task_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_memory_ids_json TEXT NOT NULL DEFAULT '[]',
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS project_dispatches (
                    id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    project_path TEXT NOT NULL DEFAULT '',
                    original_prompt TEXT NOT NULL,
                    refined_prompt TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'planned',
                    summary TEXT NOT NULL DEFAULT '',
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS companion_devices (
                    id TEXT PRIMARY KEY,
                    device_name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pairing_code_hash TEXT NOT NULL DEFAULT '',
                    pairing_secret_hash TEXT NOT NULL DEFAULT '',
                    shared_key_secret_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    paired_at TEXT,
                    last_seen_at TEXT,
                    revoked_at TEXT
                );

                CREATE TABLE IF NOT EXISTS companion_envelopes (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    ciphertext TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    opened_at TEXT
                );

                CREATE TABLE IF NOT EXISTS owner_sessions (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    revoked_at TEXT
                );

                -- FTS5 virtual table for fast memory full-text search
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(text, content=memories, content_rowid=rowid);

                -- Keep FTS5 in sync with memories table
                CREATE TRIGGER IF NOT EXISTS memories_ai
                AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_au
                AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
                    INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_ad
                AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
                END;

                CREATE TRIGGER IF NOT EXISTS audit_log_no_update
                BEFORE UPDATE ON audit_log BEGIN
                    SELECT RAISE(ABORT, 'audit_log is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
                BEFORE DELETE ON audit_log BEGIN
                    SELECT RAISE(ABORT, 'audit_log is append-only');
                END;
                """
            )
            # Backfill the FTS index only on first creation (e.g. upgrading a
            # legacy DB) rather than rebuilding on every process start.
            if not fts_existed:
                conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
            self._apply_migrations(conn)
            self._apply_agent_migrations(conn)

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        migrations = (
            (1, self._apply_phase2_companion_schema),
            (2, self._apply_phase2_relay_schema),
            (3, self._apply_phase2_relay_idempotency_schema),
        )
        self._run_migrations(conn, "schema_migrations", migrations)

    def _apply_agent_migrations(self, conn: sqlite3.Connection) -> None:
        migrations = ((1, self._apply_phase3_agent_schema),)
        self._run_migrations(conn, "agent_schema_migrations", migrations)

    def _run_migrations(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        migrations: tuple[tuple[int, Any], ...],
    ) -> None:
        if not table_name.replace("_", "").isalnum():
            raise ValueError("invalid migration table name")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for version, migration in migrations:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                applied = conn.execute(
                    f"SELECT 1 FROM {table_name} WHERE version = ?",
                    (version,),
                ).fetchone()
                if applied is not None:
                    conn.commit()
                    continue
                migration(conn)
                conn.execute(
                    f"INSERT INTO {table_name} (version) VALUES (?)",
                    (version,),
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _apply_phase3_agent_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_definitions (
                id TEXT PRIMARY KEY,
                definition_key TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                current_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_definition_versions (
                id TEXT PRIMARY KEY,
                definition_id TEXT NOT NULL REFERENCES agent_definitions(id),
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                agent_type TEXT NOT NULL,
                goal TEXT NOT NULL,
                tools_json TEXT NOT NULL DEFAULT '[]',
                memory_scope TEXT NOT NULL,
                time_budget_minutes INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                budget_json TEXT NOT NULL DEFAULT '{}',
                success_contract_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(definition_id, version)
            )
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS agent_definition_versions_no_update
            BEFORE UPDATE ON agent_definition_versions BEGIN
                SELECT RAISE(ABORT, 'agent definition versions are immutable');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS agent_definition_versions_no_delete
            BEFORE DELETE ON agent_definition_versions BEGIN
                SELECT RAISE(ABORT, 'agent definition versions are immutable');
            END
            """
        )

        agent_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(agents)").fetchall()
        }
        agent_additions = {
            "definition_id": "TEXT",
            "definition_version": "INTEGER NOT NULL DEFAULT 1",
            "definition_version_id": "TEXT",
            "parent_agent_id": "TEXT",
            "root_agent_id": "TEXT",
            "depth": "INTEGER NOT NULL DEFAULT 0",
            "budget_json": "TEXT NOT NULL DEFAULT '{}'",
            "success_contract_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, declaration in agent_additions.items():
            if name not in agent_columns:
                conn.execute(f"ALTER TABLE agents ADD COLUMN {name} {declaration}")

        run_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()
        }
        run_additions = {
            "status": "TEXT NOT NULL DEFAULT 'completed'",
            "definition_id": "TEXT",
            "definition_version": "INTEGER",
            "definition_version_id": "TEXT",
            "definition_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
            "parent_agent_id": "TEXT",
            "root_agent_id": "TEXT",
            "depth": "INTEGER NOT NULL DEFAULT 0",
            "parent_run_id": "TEXT",
            "workflow_run_id": "TEXT",
            "workflow_node_run_id": "TEXT",
            "budget_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
            "success_contract_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
            "usage_json": "TEXT NOT NULL DEFAULT '{}'",
            "evaluation_json": "TEXT NOT NULL DEFAULT '{}'",
            "started_at": "TEXT",
            "completed_at": "TEXT",
            "updated_at": "TEXT",
        }
        for name, declaration in run_additions.items():
            if name not in run_columns:
                conn.execute(f"ALTER TABLE agent_runs ADD COLUMN {name} {declaration}")

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS agents_definition_version
                ON agents(definition_id, definition_version)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS agents_parent
                ON agents(parent_agent_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS agent_runs_agent_status_started
                ON agent_runs(agent_id, status, started_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS agent_runs_workflow
                ON agent_runs(workflow_run_id, workflow_node_run_id)
            """
        )

        legacy_agents = conn.execute(
            """
            SELECT *
            FROM agents
            WHERE definition_id IS NULL
               OR definition_id = ''
               OR definition_version_id IS NULL
               OR definition_version_id = ''
            ORDER BY created_at, id
            """
        ).fetchall()
        for agent in legacy_agents:
            definition_id = str(agent["definition_id"] or self.make_id("agentdef"))
            version_id = self.make_id("agentdefver")
            created_at = str(agent["created_at"])
            budget = self.loads(agent["budget_json"] or "{}")
            if not isinstance(budget, dict):
                budget = {}
            budget.setdefault(
                "time_budget_minutes",
                int(agent["time_budget_minutes"]),
            )
            success_contract = self.loads(agent["success_contract_json"] or "{}")
            if not isinstance(success_contract, dict):
                success_contract = {}
            definition_key = f"custom:{agent['id']}"
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_definitions (
                    id, definition_key, source, current_version, created_at, updated_at
                ) VALUES (?, ?, 'custom', 1, ?, ?)
                """,
                (definition_id, definition_key, created_at, str(agent["updated_at"])),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_definition_versions (
                    id, definition_id, version, name, agent_type, goal, tools_json,
                    memory_scope, time_budget_minutes, notes, budget_json,
                    success_contract_json, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    definition_id,
                    agent["name"],
                    agent["agent_type"],
                    agent["goal"],
                    agent["tools_json"],
                    agent["memory_scope"],
                    agent["time_budget_minutes"],
                    agent["notes"],
                    self.dumps(budget),
                    self.dumps(success_contract),
                    created_at,
                ),
            )
            stored_version = conn.execute(
                """
                SELECT id
                FROM agent_definition_versions
                WHERE definition_id = ? AND version = 1
                """,
                (definition_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE agents
                SET definition_id = ?,
                    definition_version = 1,
                    definition_version_id = ?,
                    root_agent_id = COALESCE(NULLIF(root_agent_id, ''), id),
                    budget_json = ?,
                    success_contract_json = ?
                WHERE id = ?
                """,
                (
                    definition_id,
                    stored_version["id"],
                    self.dumps(budget),
                    self.dumps(success_contract),
                    agent["id"],
                ),
            )

        legacy_runs = conn.execute(
            """
            SELECT
                agent_runs.id AS run_id,
                agent_runs.outcome,
                agent_runs.created_at AS run_created_at,
                agents.*,
                agent_definition_versions.id AS version_row_id,
                agent_definition_versions.created_at AS version_created_at
            FROM agent_runs
            JOIN agents ON agents.id = agent_runs.agent_id
            LEFT JOIN agent_definition_versions
                ON agent_definition_versions.definition_id = agents.definition_id
               AND agent_definition_versions.version = agents.definition_version
            WHERE agent_runs.definition_id IS NULL
               OR agent_runs.definition_snapshot_json = '{}'
               OR agent_runs.started_at IS NULL
            """
        ).fetchall()
        for row in legacy_runs:
            definition_snapshot = {
                "definition_id": row["definition_id"],
                "definition_version": row["definition_version"],
                "definition_version_id": row["version_row_id"],
                "name": row["name"],
                "agent_type": row["agent_type"],
                "goal": row["goal"],
                "tools": self.loads(row["tools_json"]),
                "memory_scope": row["memory_scope"],
                "time_budget_minutes": row["time_budget_minutes"],
                "notes": row["notes"],
                "budget": self.loads(row["budget_json"]),
                "success_contract": self.loads(row["success_contract_json"]),
            }
            created_at = row["run_created_at"]
            conn.execute(
                """
                UPDATE agent_runs
                SET status = outcome,
                    definition_id = ?,
                    definition_version = ?,
                    definition_version_id = ?,
                    definition_snapshot_json = ?,
                    parent_agent_id = ?,
                    root_agent_id = ?,
                    depth = ?,
                    budget_snapshot_json = ?,
                    success_contract_snapshot_json = ?,
                    started_at = COALESCE(started_at, ?),
                    completed_at = COALESCE(completed_at, ?),
                    updated_at = COALESCE(updated_at, ?)
                WHERE id = ?
                """,
                (
                    row["definition_id"],
                    row["definition_version"],
                    row["version_row_id"],
                    self.dumps(definition_snapshot),
                    row["parent_agent_id"],
                    row["root_agent_id"],
                    row["depth"],
                    row["budget_json"],
                    row["success_contract_json"],
                    created_at,
                    created_at,
                    created_at,
                    row["run_id"],
                ),
            )

    def _apply_phase2_companion_schema(self, conn: sqlite3.Connection) -> None:
        device_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(companion_devices)").fetchall()
        }
        additions = {
            "protocol_version": "INTEGER NOT NULL DEFAULT 1",
            "key_agreement_public_key": "TEXT NOT NULL DEFAULT ''",
            "approval_public_key": "TEXT NOT NULL DEFAULT ''",
            "send_key_secret_name": "TEXT NOT NULL DEFAULT ''",
            "receive_key_secret_name": "TEXT NOT NULL DEFAULT ''",
            "request_key_secret_name": "TEXT NOT NULL DEFAULT ''",
            "receive_sequence": "INTEGER NOT NULL DEFAULT 0",
            "send_sequence": "INTEGER NOT NULL DEFAULT 0",
            "presence_state": "TEXT NOT NULL DEFAULT 'offline'",
            "presence_expires_at": "TEXT",
            "legacy_repair_required": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in device_columns:
                conn.execute(f"ALTER TABLE companion_devices ADD COLUMN {name} {declaration}")

        statements = (
            """
            CREATE TABLE IF NOT EXISTS companion_pairing_sessions (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                pairing_token_hash TEXT NOT NULL,
                windows_private_key_secret_name TEXT NOT NULL,
                windows_public_key TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS companion_pairing_active_device
                ON companion_pairing_sessions(device_id)
                WHERE used_at IS NULL
            """,
            """
            CREATE TABLE IF NOT EXISTS companion_request_nonces (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                request_nonce TEXT NOT NULL,
                sequence_number INTEGER NOT NULL,
                request_timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(device_id, request_nonce),
                UNIQUE(device_id, sequence_number)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS companion_request_nonces_created
                ON companion_request_nonces(created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS companion_sync_events (
                sequence_number INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                recipient_device_id TEXT,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS companion_sync_events_recipient_sequence
                ON companion_sync_events(recipient_device_id, sequence_number)
            """,
            """
            CREATE TABLE IF NOT EXISTS companion_device_cursors (
                device_id TEXT PRIMARY KEY,
                acknowledged_sequence INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS action_requests (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                action_tier INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                summary TEXT NOT NULL,
                redacted_preview_json TEXT NOT NULL DEFAULT '{}',
                frozen_payload_json TEXT NOT NULL DEFAULT '{}',
                payload_digest TEXT NOT NULL,
                originating_goal TEXT NOT NULL DEFAULT '',
                input_sources_json TEXT NOT NULL DEFAULT '[]',
                model TEXT NOT NULL DEFAULT '',
                plan_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                executed_at TEXT,
                execution_outcome TEXT NOT NULL DEFAULT '',
                execution_error TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS action_requests_status_expiry
                ON action_requests(status, expires_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS action_decisions (
                id TEXT PRIMARY KEY,
                action_request_id TEXT NOT NULL UNIQUE,
                device_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                biometric_backed INTEGER NOT NULL DEFAULT 0,
                signature TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                decided_at TEXT NOT NULL
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS companion_sync_events_no_update
            BEFORE UPDATE ON companion_sync_events BEGIN
                SELECT RAISE(ABORT, 'companion_sync_events is append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS companion_sync_events_no_delete
            BEFORE DELETE ON companion_sync_events BEGIN
                SELECT RAISE(ABORT, 'companion_sync_events is append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS action_decisions_no_update
            BEFORE UPDATE ON action_decisions BEGIN
                SELECT RAISE(ABORT, 'action_decisions is append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS action_decisions_no_delete
            BEFORE DELETE ON action_decisions BEGIN
                SELECT RAISE(ABORT, 'action_decisions is append-only');
            END
            """,
        )
        for statement in statements:
            conn.execute(statement)

    def _apply_phase2_relay_schema(self, conn: sqlite3.Connection) -> None:
        device_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(companion_devices)").fetchall()
        }
        if "relay_receive_sequence" not in device_columns:
            conn.execute(
                """
                ALTER TABLE companion_devices
                ADD COLUMN relay_receive_sequence INTEGER NOT NULL DEFAULT 0
                """
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS companion_relay_commands (
                request_message_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                command_id TEXT NOT NULL DEFAULT '',
                request_sequence INTEGER NOT NULL,
                response_envelope_json TEXT,
                response_status INTEGER,
                response_body_base64 TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(device_id, request_sequence)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS companion_relay_commands_device_status
                ON companion_relay_commands(device_id, status, updated_at)
            """
        )

    def _apply_phase2_relay_idempotency_schema(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(companion_relay_commands)"
            ).fetchall()
        }
        additions = {
            "command_id": "TEXT NOT NULL DEFAULT ''",
            "response_status": "INTEGER",
            "response_body_base64": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE companion_relay_commands "
                    f"ADD COLUMN {name} {declaration}"
                )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS companion_relay_commands_command
                ON companion_relay_commands(device_id, command_id)
                WHERE command_id <> ''
            """
        )

    @staticmethod
    def make_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, sort_keys=True)

    @staticmethod
    def loads(value: str) -> Any:
        return json.loads(value)
