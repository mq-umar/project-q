from __future__ import annotations

from typing import Any

from project_q.models import AgentCreate, AgentUpdate, utc_now
from project_q.storage import Database

BUILT_IN_TEMPLATES = {
    "research": {
        "name": "Research Agent",
        "agent_type": "research",
        "goal": "Research a given topic thoroughly and return a structured summary with key findings, sources, and actionable insights.",
        "tools": ["browser.inspect_page", "browser.run_actions", "browser.complete_goal", "knowledge.answer", "filesystem.write_file"],
        "time_budget_minutes": 30,
        "notes": "Use multiple sources. Cross-reference facts. Write findings to a report file.",
    },
    "coding": {
        "name": "Coding Agent",
        "agent_type": "coding",
        "goal": "Write, review, and improve code for a given task or specification.",
        "tools": ["shell.run_command", "filesystem.read_file", "filesystem.write_file", "filesystem.search_files", "code.generate_project"],
        "time_budget_minutes": 60,
        "notes": "Follow existing code patterns. Run tests if available. Explain changes made.",
    },
    "testing": {
        "name": "Testing Agent",
        "agent_type": "testing",
        "goal": "Run tests, analyze failures, and suggest or apply fixes.",
        "tools": ["shell.run_command", "filesystem.read_file", "filesystem.write_file", "filesystem.search_files"],
        "time_budget_minutes": 30,
        "notes": "Run pytest or the project's test runner. Capture failures and report them.",
    },
    "web_builder": {
        "name": "Web Builder Agent",
        "agent_type": "web_builder",
        "goal": "Build or improve a website or web application.",
        "tools": ["code.generate_website", "shell.run_command", "filesystem.write_file", "filesystem.read_file", "browser.inspect_page"],
        "time_budget_minutes": 90,
        "notes": "Create responsive designs. Use modern HTML/CSS/JS. Preview locally when possible.",
    },
    "monitor": {
        "name": "Monitor Agent",
        "agent_type": "monitor",
        "goal": "Watch a target (URL, file, or metric) and alert on changes or anomalies.",
        "tools": ["browser.inspect_page", "filesystem.watch_poll", "windows.notify", "knowledge.answer"],
        "time_budget_minutes": 15,
        "notes": "Take a baseline snapshot first. Compare on each run. Alert if significant change detected.",
    },
    "writer": {
        "name": "Writer Agent",
        "agent_type": "writer",
        "goal": "Draft, edit, or improve written content based on the given brief.",
        "tools": ["knowledge.answer", "filesystem.write_file", "filesystem.read_file", "browser.complete_goal"],
        "time_budget_minutes": 20,
        "notes": "Write in a clear, professional tone unless specified otherwise. Save drafts as markdown files.",
    },
    "data": {
        "name": "Data Agent",
        "agent_type": "data",
        "goal": "Analyze, transform, or visualize data from files or spreadsheets.",
        "tools": ["spreadsheet.inspect", "spreadsheet.analyze", "filesystem.read_file", "knowledge.answer", "shell.run_command"],
        "time_budget_minutes": 30,
        "notes": "Identify trends and anomalies. Generate a summary report with key numbers.",
    },
    "ops": {
        "name": "Ops Agent",
        "agent_type": "ops",
        "goal": "Perform system operations: file management, process checks, system health monitoring.",
        "tools": ["shell.run_command", "filesystem.list_directory", "windows.list_windows", "windows.notify", "filesystem.search_files"],
        "time_budget_minutes": 15,
        "notes": "Be conservative with destructive operations. Log what was done.",
    },
    "git": {
        "name": "Git Agent",
        "agent_type": "coding",
        "goal": "Manage git operations: status check, staging, commits, and pushes.",
        "tools": ["git.status", "git.add", "git.commit", "git.push", "git.diff", "git.log", "git.branch"],
        "time_budget_minutes": 10,
        "notes": "Always check status first. Write meaningful commit messages. Never force push without explicit instruction.",
    },
}


class AgentService:
    def __init__(self, db: Database, sync_service=None) -> None:
        self.db = db
        self.sync_service = sync_service

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
        created = self.get(agent_id)
        self._emit("created", created)
        return created

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
        record = self.get(agent_id)
        self._emit("updated", record)
        return record

    def delete(self, agent_id: str) -> None:
        with self.db.connection() as conn:
            conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        self._emit("deleted", {"id": agent_id})

    def _emit(self, operation: str, payload: dict[str, Any]) -> None:
        if self.sync_service is not None:
            self.sync_service.append(
                resource_type="agent",
                resource_id=str(payload["id"]),
                operation=operation,
                payload=payload,
            )

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

    def list_templates(self) -> list[dict]:
        return [{"id": k, **v} for k, v in BUILT_IN_TEMPLATES.items()]

    def spawn_from_template(self, template_id: str, goal_override: str = "") -> dict:
        template = BUILT_IN_TEMPLATES.get(template_id)
        if not template:
            raise KeyError(f"unknown template: {template_id}")
        from project_q.models import AgentCreate
        payload = AgentCreate(
            name=template["name"],
            agent_type=template["agent_type"],
            goal=goal_override or template["goal"],
            tools=template["tools"],
            status="active",
            time_budget_minutes=template["time_budget_minutes"],
            notes=template["notes"],
        )
        return self.create(payload)
