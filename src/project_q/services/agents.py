from __future__ import annotations

from typing import Any

from project_q.models import AgentCreate, AgentUpdate, utc_now
from project_q.storage import Database


class AgentService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, payload: AgentCreate) -> dict[str, Any]:
        agent_id = self.db.make_id("agent")
        now = utc_now()
        record = payload.model_dump()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO agents (
                    id, name, agent_type, goal, status, tools_json,
                    memory_scope, time_budget_minutes, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    record["name"],
                    record["agent_type"],
                    record["goal"],
                    record["status"],
                    self.db.dumps(record["tools"]),
                    record["memory_scope"],
                    record["time_budget_minutes"],
                    record["notes"],
                    now,
                    now,
                ),
            )
        return self.get(agent_id)

    def list_all(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
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
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get(self, agent_id: str) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute(
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
                WHERE id = ?
                """,
                (agent_id,),
            ).fetchone()
        if row is None:
            raise KeyError(agent_id)
        return self._row_to_dict(row)

    def update(self, agent_id: str, payload: AgentUpdate) -> dict[str, Any]:
        current = self.get(agent_id)
        updated = {**current, **payload.model_dump(exclude_none=True)}
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE agents
                SET name = ?, agent_type = ?, goal = ?, status = ?,
                    tools_json = ?, memory_scope = ?, time_budget_minutes = ?,
                    notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated["name"],
                    updated["agent_type"],
                    updated["goal"],
                    updated["status"],
                    self.db.dumps(updated["tools"]),
                    updated["memory_scope"],
                    updated["time_budget_minutes"],
                    updated["notes"],
                    utc_now(),
                    agent_id,
                ),
            )
        return self.get(agent_id)

    def delete(self, agent_id: str) -> None:
        with self.db.connection() as conn:
            conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "agent_type": row["agent_type"],
            "goal": row["goal"],
            "status": row["status"],
            "tools": self.db.loads(row["tools_json"]),
            "memory_scope": row["memory_scope"],
            "time_budget_minutes": row["time_budget_minutes"],
            "notes": row["notes"],
            "last_run_at": row["last_run_at"] if "last_run_at" in row.keys() else None,
            "last_run_outcome": row["last_run_outcome"] if "last_run_outcome" in row.keys() else None,
            "last_run_mode": row["last_run_mode"] if "last_run_mode" in row.keys() else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
