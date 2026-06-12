from __future__ import annotations

from typing import Any

from project_q.models import utc_now


_SECRET_KEY_MARKERS = {
    "api_key",
    "authorization",
    "credential",
    "password",
    "private_key",
    "request_key",
    "secret",
    "shared_key",
    "token",
}


class SyncEventService:
    def __init__(self, db) -> None:
        self.db = db

    def append(
        self,
        *,
        resource_type: str,
        resource_id: str,
        operation: str,
        payload: dict[str, Any],
        recipient_device_id: str | None = None,
    ) -> dict[str, Any]:
        event_id = self.db.make_id("sync")
        now = utc_now()
        safe_payload = self._redact(payload)
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO companion_sync_events (
                    id, recipient_device_id, resource_type, resource_id,
                    operation, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    recipient_device_id,
                    self._required_text(resource_type, "resource type", 100),
                    self._required_text(resource_id, "resource ID", 300),
                    self._required_text(operation, "operation", 100),
                    self.db.dumps(safe_payload),
                    now,
                ),
            )
            sequence_number = int(cursor.lastrowid)
        return {
            "id": event_id,
            "sequence_number": sequence_number,
            "recipient_device_id": recipient_device_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "operation": operation,
            "payload": safe_payload,
            "created_at": now,
        }

    def pull(
        self,
        device_id: str,
        *,
        after_sequence: int,
        limit: int = 200,
    ) -> dict[str, Any]:
        self._active_device(device_id)
        cursor_value = max(0, int(after_sequence))
        bounded_limit = max(1, min(int(limit), 500))
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM companion_sync_events
                WHERE sequence_number > ?
                  AND (recipient_device_id IS NULL OR recipient_device_id = ?)
                ORDER BY sequence_number
                LIMIT ?
                """,
                (cursor_value, device_id, bounded_limit),
            ).fetchall()
            cursor_row = conn.execute(
                """
                SELECT acknowledged_sequence
                FROM companion_device_cursors
                WHERE device_id = ?
                """,
                (device_id,),
            ).fetchone()
        items = [self._row_to_dict(row) for row in rows]
        return {
            "items": items,
            "after_sequence": cursor_value,
            "next_sequence": items[-1]["sequence_number"] if items else cursor_value,
            "acknowledged_sequence": (
                int(cursor_row["acknowledged_sequence"]) if cursor_row is not None else 0
            ),
            "has_more": len(items) == bounded_limit,
            "snapshot_required": False,
        }

    def snapshot(
        self,
        device_id: str,
        *,
        limits: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        self._active_device(device_id)
        requested = limits or {}

        def bound(name: str, default: int = 100) -> int:
            return max(1, min(int(requested.get(name, default)), 200))

        with self.db.connection() as conn:
            sequence_row = conn.execute(
                """
                SELECT COALESCE(MAX(sequence_number), 0) AS maximum
                FROM companion_sync_events
                WHERE recipient_device_id IS NULL OR recipient_device_id = ?
                """,
                (device_id,),
            ).fetchone()
            cursor_row = conn.execute(
                """
                SELECT acknowledged_sequence
                FROM companion_device_cursors
                WHERE device_id = ?
                """,
                (device_id,),
            ).fetchone()
            conversations = conn.execute(
                """
                SELECT id, role, content, channel, metadata_json, created_at
                FROM conversations
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (bound("conversations"),),
            ).fetchall()
            memories = conn.execute(
                "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?",
                (bound("memories"),),
            ).fetchall()
            tasks = conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?",
                (bound("tasks"),),
            ).fetchall()
            agents = conn.execute(
                """
                SELECT
                    agents.*,
                    (
                        SELECT created_at
                        FROM agent_runs
                        WHERE agent_runs.agent_id = agents.id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS last_run_at,
                    (
                        SELECT outcome
                        FROM agent_runs
                        WHERE agent_runs.agent_id = agents.id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS last_run_outcome,
                    (
                        SELECT reasoning_mode
                        FROM agent_runs
                        WHERE agent_runs.agent_id = agents.id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS last_run_mode
                FROM agents
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (bound("agents"),),
            ).fetchall()
            routines = conn.execute(
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
                (bound("routines"),),
            ).fetchall()
            approvals = conn.execute(
                """
                SELECT id, session_id, action_tier, tool_name, summary,
                       redacted_preview_json, payload_digest, originating_goal,
                       input_sources_json, model, plan_id, status, expires_at,
                       created_at, updated_at, executed_at, execution_outcome,
                       execution_error
                FROM action_requests
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (bound("approvals"),),
            ).fetchall()
            audit = conn.execute(
                """
                SELECT *
                FROM audit_log
                ORDER BY timestamp DESC, rowid DESC
                LIMIT ?
                """,
                (bound("audit"),),
            ).fetchall()
            setting_rows = conn.execute(
                """
                SELECT key, value_json
                FROM settings
                WHERE key IN (
                    'kill_switch_active',
                    'kill_switch_reason',
                    'kill_switch_activated_at',
                    'kill_switch_source'
                )
                """
            ).fetchall()

        settings = {
            row["key"]: self.db.loads(row["value_json"])
            for row in setting_rows
        }
        return {
            "device_id": device_id,
            "snapshot_sequence": int(sequence_row["maximum"]),
            "acknowledged_sequence": (
                int(cursor_row["acknowledged_sequence"]) if cursor_row is not None else 0
            ),
            "generated_at": utc_now(),
            "conversations": [
                {
                    "id": row["id"],
                    "role": row["role"],
                    "content": row["content"],
                    "channel": row["channel"],
                    "metadata": self._redact(self.db.loads(row["metadata_json"])),
                    "created_at": row["created_at"],
                }
                for row in conversations
            ],
            "memories": [
                {
                    "id": row["id"],
                    "text": row["text"],
                    "kind": row["kind"],
                    "source": row["source"],
                    "confidence": row["confidence"],
                    "owner_confirmed": bool(row["owner_confirmed"]),
                    "tags": self.db.loads(row["tags_json"]),
                    "metadata": self._redact(self.db.loads(row["metadata_json"])),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in memories
            ],
            "tasks": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "description": row["description"],
                    "priority": row["priority"],
                    "status": row["status"],
                    "due_at": row["due_at"],
                    "source": row["source"],
                    "metadata": self._redact(self.db.loads(row["metadata_json"])),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in tasks
            ],
            "agents": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "agent_type": row["agent_type"],
                    "goal": row["goal"],
                    "status": row["status"],
                    "tools": self.db.loads(row["tools_json"]),
                    "memory_scope": row["memory_scope"],
                    "time_budget_minutes": row["time_budget_minutes"],
                    "notes": row["notes"],
                    "last_run_at": row["last_run_at"],
                    "last_run_outcome": row["last_run_outcome"],
                    "last_run_mode": row["last_run_mode"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in agents
            ],
            "routines": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "goal": row["goal"],
                    "description": row["description"],
                    "status": row["status"],
                    "trigger_type": row["trigger_type"],
                    "trusted": bool(row["trusted"]),
                    "tools": self.db.loads(row["tools_json"]),
                    "steps": self._redact(self.db.loads(row["steps_json"])),
                    "notes": row["notes"],
                    "last_run_at": row["last_run_at"],
                    "last_run_outcome": row["last_run_outcome"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in routines
            ],
            "approvals": [
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "action_tier": int(row["action_tier"]),
                    "tool_name": row["tool_name"],
                    "summary": row["summary"],
                    "redacted_preview": self.db.loads(row["redacted_preview_json"]),
                    "payload_digest": row["payload_digest"],
                    "originating_goal": row["originating_goal"],
                    "input_sources": self.db.loads(row["input_sources_json"]),
                    "model": row["model"],
                    "plan_id": row["plan_id"],
                    "status": row["status"],
                    "expires_at": row["expires_at"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "executed_at": row["executed_at"],
                    "execution_outcome": row["execution_outcome"],
                    "execution_error": row["execution_error"],
                }
                for row in approvals
            ],
            "audit": [
                {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "action_type": row["action_type"],
                    "action_tier": int(row["action_tier"]),
                    "tool_name": row["tool_name"],
                    "model": row["model"],
                    "input_sources": self.db.loads(row["input_sources_json"]),
                    "approved_by_owner": bool(row["approved_by_owner"]),
                    "outcome": row["outcome"],
                    "error": row["error"],
                    "metadata": self._redact(
                        self.db.loads(row["metadata_json"])
                    ),
                }
                for row in audit
            ],
            "control": {
                "active": bool(settings.get("kill_switch_active", False)),
                "reason": str(settings.get("kill_switch_reason", "") or ""),
                "activated_at": str(settings.get("kill_switch_activated_at", "") or ""),
                "source": str(settings.get("kill_switch_source", "") or ""),
            },
        }

    def acknowledge(self, device_id: str, *, sequence_number: int) -> dict[str, Any]:
        self._active_device(device_id)
        sequence = max(0, int(sequence_number))
        now = utc_now()
        with self.db.connection() as conn:
            max_row = conn.execute(
                """
                SELECT COALESCE(MAX(sequence_number), 0) AS maximum
                FROM companion_sync_events
                WHERE recipient_device_id IS NULL OR recipient_device_id = ?
                """,
                (device_id,),
            ).fetchone()
            maximum = int(max_row["maximum"])
            if sequence > maximum:
                raise ValueError("cannot acknowledge an unseen sync sequence")
            conn.execute(
                """
                INSERT INTO companion_device_cursors (
                    device_id, acknowledged_sequence, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    acknowledged_sequence = MAX(
                        companion_device_cursors.acknowledged_sequence,
                        excluded.acknowledged_sequence
                    ),
                    updated_at = excluded.updated_at
                """,
                (device_id, sequence, now),
            )
            row = conn.execute(
                """
                SELECT acknowledged_sequence, updated_at
                FROM companion_device_cursors
                WHERE device_id = ?
                """,
                (device_id,),
            ).fetchone()
        return {
            "device_id": device_id,
            "acknowledged_sequence": int(row["acknowledged_sequence"]),
            "updated_at": row["updated_at"],
        }

    def _active_device(self, device_id: str):
        with self.db.connection() as conn:
            row = conn.execute(
                """
                SELECT id, status, protocol_version, legacy_repair_required
                FROM companion_devices
                WHERE id = ?
                """,
                (device_id,),
            ).fetchone()
        if (
            row is None
            or row["status"] != "paired"
            or int(row["protocol_version"]) != 2
            or bool(row["legacy_repair_required"])
        ):
            raise PermissionError("companion device is not active")
        return row

    def _row_to_dict(self, row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "sequence_number": int(row["sequence_number"]),
            "recipient_device_id": row["recipient_device_id"],
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "operation": row["operation"],
            "payload": self.db.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }

    @classmethod
    def _redact(cls, value: Any, *, key_name: str = "") -> Any:
        lowered = key_name.lower()
        if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                str(key): cls._redact(item, key_name=str(key))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        return value

    @staticmethod
    def _required_text(value: Any, label: str, maximum: int) -> str:
        text = str(value).strip()
        if not text or len(text) > maximum:
            raise ValueError(f"invalid {label}")
        return text
