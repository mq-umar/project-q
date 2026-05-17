from __future__ import annotations

from typing import Any

from project_q.models import AgentUpdate, utc_now


class AgentRunnerService:
    def __init__(self, db, agent_service, context_service, reasoner_service, executor_service, audit_service) -> None:
        self.db = db
        self.agent_service = agent_service
        self.context_service = context_service
        self.reasoner_service = reasoner_service
        self.executor_service = executor_service
        self.audit_service = audit_service

    def run(self, agent_id: str, *, owner_approved: bool) -> dict[str, Any]:
        agent = self.agent_service.get(agent_id)
        self.agent_service.update(agent_id, AgentUpdate(status="running"))
        instruction = self._build_instruction(agent)
        context = self.context_service.build(instruction)
        context["active_agent"] = agent

        try:
            reasoning = self.reasoner_service.plan(user_message=instruction, context=context)
            execution = self.executor_service.execute(
                plan=reasoning.plan,
                owner_approved=owner_approved,
                input_sources=["agent", "memory", "tasks", "owner"],
            )
            reply = self.executor_service.compose_reply(
                reasoning.plan.reply,
                execution["executed_tools"],
                execution["blocked_tools"],
                reasoning.warning,
            )
            record = self._store_run(
                agent_id=agent_id,
                goal=agent["goal"],
                reply=reply,
                outcome="completed",
                reasoning_mode=reasoning.mode,
                model_name=reasoning.model_name or "",
                created_task_ids=execution["created_task_ids"],
                created_memory_ids=execution["created_memory_ids"],
                created_agent_ids=execution["created_agent_ids"],
                executed_tools=execution["executed_tools"],
                blocked_tools=execution["blocked_tools"],
                warning=reasoning.warning or "",
            )
            self.agent_service.update(agent_id, AgentUpdate(status="active"))
            self.audit_service.log(
                action_type="agent_run",
                action_tier=1,
                tool_name="agent_runner",
                outcome="completed",
                approved_by_owner=owner_approved,
                input_sources=["agent", "memory", "tasks", "owner"],
                metadata={"agent_id": agent_id, "run_id": record["id"]},
            )
            return record
        except Exception as exc:  # noqa: BLE001
            self.agent_service.update(agent_id, AgentUpdate(status="blocked"))
            record = self._store_run(
                agent_id=agent_id,
                goal=agent["goal"],
                reply="",
                outcome="failed",
                reasoning_mode="error",
                model_name="",
                created_task_ids=[],
                created_memory_ids=[],
                created_agent_ids=[],
                executed_tools=[],
                blocked_tools=[],
                warning=str(exc),
            )
            self.audit_service.log(
                action_type="agent_run",
                action_tier=1,
                tool_name="agent_runner",
                outcome="failed",
                approved_by_owner=owner_approved,
                input_sources=["agent", "memory", "tasks", "owner"],
                metadata={"agent_id": agent_id, "run_id": record["id"]},
                error=str(exc),
            )
            raise

    def list_runs(self, agent_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM agent_runs
                WHERE agent_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def _store_run(
        self,
        *,
        agent_id: str,
        goal: str,
        reply: str,
        outcome: str,
        reasoning_mode: str,
        model_name: str,
        created_task_ids: list[str],
        created_memory_ids: list[str],
        created_agent_ids: list[str],
        executed_tools: list[dict[str, Any]],
        blocked_tools: list[dict[str, Any]],
        warning: str,
    ) -> dict[str, Any]:
        run_id = self.db.make_id("agentrun")
        created_at = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs (
                    id, agent_id, goal, reply, outcome, reasoning_mode, model_name,
                    created_task_ids_json, created_memory_ids_json, created_agent_ids_json,
                    executed_tools_json, blocked_tools_json, warning, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    agent_id,
                    goal,
                    reply,
                    outcome,
                    reasoning_mode,
                    model_name,
                    self.db.dumps(created_task_ids),
                    self.db.dumps(created_memory_ids),
                    self.db.dumps(created_agent_ids),
                    self.db.dumps(executed_tools),
                    self.db.dumps(blocked_tools),
                    warning,
                    created_at,
                ),
            )
        return self.list_runs(agent_id, limit=1)[0]

    def _build_instruction(self, agent: dict[str, Any]) -> str:
        tool_text = ", ".join(agent["tools"]) if agent["tools"] else "no preference provided"
        extra_guidance = ""
        lowered_goal = agent["goal"].lower()
        if any(phrase in lowered_goal for phrase in ("open browser", "open the browser", "google", "search")):
            extra_guidance = (
                "\nIf the goal is a visible browser search or opening a page for the owner to see, "
                "prefer windows.open_url. Use browser.run_actions for interactive browser steps "
                "and browser.inspect_page only for read-only inspection."
            )
        return (
            f"Run the agent `{agent['name']}`.\n"
            f"Agent type: {agent['agent_type']}.\n"
            f"Goal: {agent['goal']}\n"
            f"Allowed tools: {tool_text}.\n"
            f"Notes: {agent['notes'] or 'none'}.\n"
            "Work the goal, create any needed tasks or memories, and use tools only when useful."
            f"{extra_guidance}"
        )

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "goal": row["goal"],
            "reply": row["reply"],
            "outcome": row["outcome"],
            "reasoning_mode": row["reasoning_mode"],
            "model_name": row["model_name"] or None,
            "created_task_ids": self.db.loads(row["created_task_ids_json"]),
            "created_memory_ids": self.db.loads(row["created_memory_ids_json"]),
            "created_agent_ids": self.db.loads(row["created_agent_ids_json"]),
            "executed_tools": self.db.loads(row["executed_tools_json"]),
            "blocked_tools": self.db.loads(row["blocked_tools_json"]),
            "warning": row["warning"] or None,
            "created_at": row["created_at"],
        }
