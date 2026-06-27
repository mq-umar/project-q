from __future__ import annotations

from typing import Any

from project_q.models import AgentCreate, AgentUpdate, utc_now
from project_q.storage import Database

BUILT_IN_TEMPLATES = {
    "research": {
        "name": "Research Agent",
        "agent_type": "research",
        "goal": "Research a given topic thoroughly and return a structured summary with key findings, sources, and actionable insights.",
        "tools": ["research.web", "browser.inspect_page", "browser.run_actions", "browser.complete_goal", "knowledge.answer", "filesystem.write_file"],
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
    DEFINITION_FIELDS = (
        "name",
        "agent_type",
        "goal",
        "tools",
        "memory_scope",
        "time_budget_minutes",
        "notes",
        "budget",
        "success_contract",
    )

    def __init__(self, db: Database, sync_service=None) -> None:
        self.db = db
        self.sync_service = sync_service
        self._ensure_builtin_definitions()

    def create(self, payload: AgentCreate) -> dict[str, Any]:
        agent_id = self.db.make_id("agent")
        now = utc_now()
        record = payload.model_dump()
        record["budget"] = self._normalize_budget(
            record.get("budget"),
            record["time_budget_minutes"],
        )
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            definition_id = record.get("definition_id")
            requested_version = record.get("definition_version")
            if definition_id:
                version = self._load_definition_version(
                    conn,
                    definition_id,
                    requested_version,
                )
                for field in self.DEFINITION_FIELDS:
                    record[field] = version[field]
                definition_version = int(version["version"])
                definition_version_id = str(version["id"])
            else:
                definition_id, definition_version, definition_version_id = (
                    self._create_custom_definition(
                        conn,
                        agent_id=agent_id,
                        record=record,
                        now=now,
                    )
                )
            parent_agent_id, root_agent_id, depth = self._resolve_hierarchy(
                conn,
                agent_id=agent_id,
                parent_agent_id=record.get("parent_agent_id"),
            )
            conn.execute(
                """
                INSERT INTO agents (
                    id, name, agent_type, goal, status, tools_json,
                    memory_scope, time_budget_minutes, notes,
                    definition_id, definition_version, definition_version_id,
                    parent_agent_id, root_agent_id, depth, budget_json,
                    success_contract_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    definition_id,
                    definition_version,
                    definition_version_id,
                    parent_agent_id,
                    root_agent_id,
                    depth,
                    self.db.dumps(record["budget"]),
                    self.db.dumps(record["success_contract"]),
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
                        ORDER BY COALESCE(started_at, created_at) DESC, rowid DESC
                        LIMIT 1
                    ) AS last_run_at,
                    (
                        SELECT outcome
                        FROM agent_runs
                        WHERE agent_runs.agent_id = agents.id
                        ORDER BY COALESCE(started_at, created_at) DESC, rowid DESC
                        LIMIT 1
                    ) AS last_run_outcome,
                    (
                        SELECT reasoning_mode
                        FROM agent_runs
                        WHERE agent_runs.agent_id = agents.id
                        ORDER BY COALESCE(started_at, created_at) DESC, rowid DESC
                        LIMIT 1
                    ) AS last_run_mode,
                    (
                        SELECT COUNT(*)
                        FROM agent_runs
                        WHERE agent_runs.agent_id = agents.id
                          AND agent_runs.status IN ('queued', 'running', 'cancelling')
                    ) AS active_run_count
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
                        ORDER BY COALESCE(started_at, created_at) DESC, rowid DESC
                        LIMIT 1
                    ) AS last_run_at,
                    (
                        SELECT outcome
                        FROM agent_runs
                        WHERE agent_runs.agent_id = agents.id
                        ORDER BY COALESCE(started_at, created_at) DESC, rowid DESC
                        LIMIT 1
                    ) AS last_run_outcome,
                    (
                        SELECT reasoning_mode
                        FROM agent_runs
                        WHERE agent_runs.agent_id = agents.id
                        ORDER BY COALESCE(started_at, created_at) DESC, rowid DESC
                        LIMIT 1
                    ) AS last_run_mode,
                    (
                        SELECT COUNT(*)
                        FROM agent_runs
                        WHERE agent_runs.agent_id = agents.id
                          AND agent_runs.status IN ('queued', 'running', 'cancelling')
                    ) AS active_run_count
                FROM agents
                WHERE id = ?
                """,
                (agent_id,),
            ).fetchone()
        if row is None:
            raise KeyError(agent_id)
        return self._row_to_dict(row)

    def update(self, agent_id: str, payload: AgentUpdate) -> dict[str, Any]:
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(agent_id)
            current = self._row_to_dict(row)
            current["status"] = row["status"]
            changes = payload.model_dump(exclude_unset=True)
            updated = {**current, **changes}
            if "time_budget_minutes" in changes and "budget" not in changes:
                updated["budget"] = dict(current["budget"])
                updated["budget"]["time_budget_minutes"] = updated[
                    "time_budget_minutes"
                ]
            else:
                updated["budget"] = self._normalize_budget(
                    updated.get("budget"),
                    updated["time_budget_minutes"],
                )

            definition_id = str(row["definition_id"])
            definition_version = int(row["definition_version"])
            definition_version_id = str(row["definition_version_id"])
            if any(
                field in changes and updated[field] != current[field]
                for field in self.DEFINITION_FIELDS
            ):
                definition_id, definition_version, definition_version_id = (
                    self._append_definition_version(
                        conn,
                        agent_id=agent_id,
                        definition_id=definition_id,
                        record=updated,
                    )
                )

            parent_agent_id, root_agent_id, depth = self._resolve_hierarchy(
                conn,
                agent_id=agent_id,
                parent_agent_id=updated.get("parent_agent_id"),
            )
            conn.execute(
                """
                UPDATE agents
                SET name = ?, agent_type = ?, goal = ?, status = ?,
                    tools_json = ?, memory_scope = ?, time_budget_minutes = ?,
                    notes = ?, definition_id = ?, definition_version = ?,
                    definition_version_id = ?, parent_agent_id = ?,
                    root_agent_id = ?, depth = ?, budget_json = ?,
                    success_contract_json = ?, updated_at = ?
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
                    definition_id,
                    definition_version,
                    definition_version_id,
                    parent_agent_id,
                    root_agent_id,
                    depth,
                    self.db.dumps(updated["budget"]),
                    self.db.dumps(updated["success_contract"]),
                    utc_now(),
                    agent_id,
                ),
            )
            self._rebase_descendants(
                conn,
                agent_id=agent_id,
                root_agent_id=root_agent_id,
                depth=depth,
            )
        record = self.get(agent_id)
        self._emit("updated", record)
        return record

    def delete(self, agent_id: str) -> None:
        with self.db.connection() as conn:
            children = conn.execute(
                "SELECT id FROM agents WHERE parent_agent_id = ?",
                (agent_id,),
            ).fetchall()
            for child in children:
                conn.execute(
                    """
                    UPDATE agents
                    SET parent_agent_id = NULL, root_agent_id = id, depth = 0,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), child["id"]),
                )
                self._rebase_descendants(
                    conn,
                    agent_id=child["id"],
                    root_agent_id=child["id"],
                    depth=0,
                )
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
        keys = set(row.keys())
        budget = (
            self.db.loads(row["budget_json"])
            if "budget_json" in keys and row["budget_json"]
            else {"time_budget_minutes": row["time_budget_minutes"]}
        )
        success_contract = (
            self.db.loads(row["success_contract_json"])
            if "success_contract_json" in keys and row["success_contract_json"]
            else {}
        )
        active_run_count = (
            int(row["active_run_count"] or 0)
            if "active_run_count" in keys
            else 0
        )
        return {
            "id": row["id"],
            "name": row["name"],
            "agent_type": row["agent_type"],
            "goal": row["goal"],
            "status": "running" if active_run_count else row["status"],
            "tools": self.db.loads(row["tools_json"]),
            "memory_scope": row["memory_scope"],
            "time_budget_minutes": row["time_budget_minutes"],
            "notes": row["notes"],
            "definition_id": row["definition_id"] if "definition_id" in keys else None,
            "definition_version": (
                row["definition_version"]
                if "definition_version" in keys
                else None
            ),
            "definition_version_id": (
                row["definition_version_id"]
                if "definition_version_id" in keys
                else None
            ),
            "parent_agent_id": (
                row["parent_agent_id"]
                if "parent_agent_id" in keys
                else None
            ),
            "root_agent_id": (
                row["root_agent_id"]
                if "root_agent_id" in keys
                else row["id"]
            ),
            "depth": row["depth"] if "depth" in keys else 0,
            "budget": budget,
            "success_contract": success_contract,
            "last_run_at": row["last_run_at"] if "last_run_at" in keys else None,
            "last_run_outcome": row["last_run_outcome"] if "last_run_outcome" in keys else None,
            "last_run_mode": row["last_run_mode"] if "last_run_mode" in keys else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_templates(self) -> list[dict]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT definition_key, id, current_version
                FROM agent_definitions
                WHERE source = 'builtin'
                """
            ).fetchall()
        definitions = {
            row["definition_key"]: {
                "definition_id": row["id"],
                "definition_version": row["current_version"],
            }
            for row in rows
        }
        return [
            {"id": key, **template, **definitions[key]}
            for key, template in BUILT_IN_TEMPLATES.items()
        ]

    def spawn_from_template(self, template_id: str, goal_override: str = "") -> dict:
        template = BUILT_IN_TEMPLATES.get(template_id)
        if not template:
            raise KeyError(f"unknown template: {template_id}")
        definition = next(
            item for item in self.list_templates() if item["id"] == template_id
        )
        payload = AgentCreate(
            name=template["name"],
            agent_type=template["agent_type"],
            goal=goal_override or template["goal"],
            tools=template["tools"],
            status="active",
            time_budget_minutes=template["time_budget_minutes"],
            notes=template["notes"],
            definition_id=(
                None if goal_override else definition["definition_id"]
            ),
            definition_version=(
                None if goal_override else definition["definition_version"]
            ),
        )
        return self.create(payload)

    def get_definition_version(
        self,
        definition_id: str,
        version: int | None = None,
    ) -> dict[str, Any]:
        with self.db.connection() as conn:
            return self._load_definition_version(conn, definition_id, version)

    def _ensure_builtin_definitions(self) -> None:
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for key, template in BUILT_IN_TEMPLATES.items():
                definition_id = f"agentdef_builtin_{key}"
                version_id = f"agentdefver_builtin_{key}_v1"
                now = utc_now()
                budget = {
                    "time_budget_minutes": template["time_budget_minutes"],
                }
                conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_definitions (
                        id, definition_key, source, current_version,
                        created_at, updated_at
                    ) VALUES (?, ?, 'builtin', 1, ?, ?)
                    """,
                    (definition_id, key, now, now),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_definition_versions (
                        id, definition_id, version, name, agent_type, goal,
                        tools_json, memory_scope, time_budget_minutes, notes,
                        budget_json, success_contract_json, created_at
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, 'task-local', ?, ?, ?, '{}', ?)
                    """,
                    (
                        version_id,
                        definition_id,
                        template["name"],
                        template["agent_type"],
                        template["goal"],
                        self.db.dumps(template["tools"]),
                        template["time_budget_minutes"],
                        template["notes"],
                        self.db.dumps(budget),
                        now,
                    ),
                )

    def _create_custom_definition(
        self,
        conn,
        *,
        agent_id: str,
        record: dict[str, Any],
        now: str,
    ) -> tuple[str, int, str]:
        definition_id = self.db.make_id("agentdef")
        version_id = self.db.make_id("agentdefver")
        conn.execute(
            """
            INSERT INTO agent_definitions (
                id, definition_key, source, current_version, created_at, updated_at
            ) VALUES (?, ?, 'custom', 1, ?, ?)
            """,
            (definition_id, f"custom:{agent_id}", now, now),
        )
        self._insert_definition_version(
            conn,
            version_id=version_id,
            definition_id=definition_id,
            version=1,
            record=record,
            created_at=now,
        )
        return definition_id, 1, version_id

    def _append_definition_version(
        self,
        conn,
        *,
        agent_id: str,
        definition_id: str,
        record: dict[str, Any],
    ) -> tuple[str, int, str]:
        definition = conn.execute(
            "SELECT * FROM agent_definitions WHERE id = ?",
            (definition_id,),
        ).fetchone()
        if definition is None:
            raise KeyError(definition_id)
        now = utc_now()
        if definition["source"] == "builtin":
            return self._create_custom_definition(
                conn,
                agent_id=agent_id,
                record=record,
                now=now,
            )
        version = int(definition["current_version"]) + 1
        version_id = self.db.make_id("agentdefver")
        self._insert_definition_version(
            conn,
            version_id=version_id,
            definition_id=definition_id,
            version=version,
            record=record,
            created_at=now,
        )
        conn.execute(
            """
            UPDATE agent_definitions
            SET current_version = ?, updated_at = ?
            WHERE id = ?
            """,
            (version, now, definition_id),
        )
        return definition_id, version, version_id

    def _insert_definition_version(
        self,
        conn,
        *,
        version_id: str,
        definition_id: str,
        version: int,
        record: dict[str, Any],
        created_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO agent_definition_versions (
                id, definition_id, version, name, agent_type, goal, tools_json,
                memory_scope, time_budget_minutes, notes, budget_json,
                success_contract_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                definition_id,
                version,
                record["name"],
                record["agent_type"],
                record["goal"],
                self.db.dumps(record["tools"]),
                record["memory_scope"],
                record["time_budget_minutes"],
                record["notes"],
                self.db.dumps(record["budget"]),
                self.db.dumps(record["success_contract"]),
                created_at,
            ),
        )

    def _load_definition_version(
        self,
        conn,
        definition_id: str,
        version: int | None,
    ) -> dict[str, Any]:
        if version is None:
            definition = conn.execute(
                "SELECT current_version FROM agent_definitions WHERE id = ?",
                (definition_id,),
            ).fetchone()
            if definition is None:
                raise KeyError(definition_id)
            version = int(definition["current_version"])
        row = conn.execute(
            """
            SELECT *
            FROM agent_definition_versions
            WHERE definition_id = ? AND version = ?
            """,
            (definition_id, version),
        ).fetchone()
        if row is None:
            raise KeyError(f"{definition_id}@{version}")
        return {
            "id": row["id"],
            "definition_id": row["definition_id"],
            "version": row["version"],
            "name": row["name"],
            "agent_type": row["agent_type"],
            "goal": row["goal"],
            "tools": self.db.loads(row["tools_json"]),
            "memory_scope": row["memory_scope"],
            "time_budget_minutes": row["time_budget_minutes"],
            "notes": row["notes"],
            "budget": self.db.loads(row["budget_json"]),
            "success_contract": self.db.loads(row["success_contract_json"]),
            "created_at": row["created_at"],
        }

    def _resolve_hierarchy(
        self,
        conn,
        *,
        agent_id: str,
        parent_agent_id: str | None,
    ) -> tuple[str | None, str, int]:
        if not parent_agent_id:
            return None, agent_id, 0
        if parent_agent_id == agent_id:
            raise ValueError("an agent cannot be its own parent")
        parent = conn.execute(
            """
            SELECT id, parent_agent_id, root_agent_id, depth
            FROM agents
            WHERE id = ?
            """,
            (parent_agent_id,),
        ).fetchone()
        if parent is None:
            raise KeyError(parent_agent_id)
        ancestor = parent
        while ancestor is not None:
            if ancestor["id"] == agent_id:
                raise ValueError("agent hierarchy cannot contain a cycle")
            next_parent = ancestor["parent_agent_id"]
            if not next_parent:
                break
            ancestor = conn.execute(
                """
                SELECT id, parent_agent_id, root_agent_id, depth
                FROM agents
                WHERE id = ?
                """,
                (next_parent,),
            ).fetchone()
        return (
            parent_agent_id,
            str(parent["root_agent_id"] or parent["id"]),
            int(parent["depth"]) + 1,
        )

    def _rebase_descendants(
        self,
        conn,
        *,
        agent_id: str,
        root_agent_id: str,
        depth: int,
    ) -> None:
        children = conn.execute(
            "SELECT id FROM agents WHERE parent_agent_id = ?",
            (agent_id,),
        ).fetchall()
        for child in children:
            child_depth = depth + 1
            conn.execute(
                """
                UPDATE agents
                SET root_agent_id = ?, depth = ?, updated_at = ?
                WHERE id = ?
                """,
                (root_agent_id, child_depth, utc_now(), child["id"]),
            )
            self._rebase_descendants(
                conn,
                agent_id=child["id"],
                root_agent_id=root_agent_id,
                depth=child_depth,
            )

    @staticmethod
    def _normalize_budget(
        budget: dict[str, Any] | None,
        time_budget_minutes: int,
    ) -> dict[str, Any]:
        normalized = dict(budget or {})
        normalized.setdefault("time_budget_minutes", time_budget_minutes)
        return normalized
