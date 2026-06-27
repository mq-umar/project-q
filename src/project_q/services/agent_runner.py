from __future__ import annotations

import time
from typing import Any

from project_q.models import utc_now


class AgentRunnerService:
    def __init__(self, db, agent_service, context_service, reasoner_service, executor_service, audit_service) -> None:
        self.db = db
        self.agent_service = agent_service
        self.context_service = context_service
        self.reasoner_service = reasoner_service
        self.executor_service = executor_service
        self.audit_service = audit_service

    def run(
        self,
        agent_id: str,
        *,
        owner_approved: bool,
        parent_run_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_node_run_id: str | None = None,
        budget: dict[str, Any] | None = None,
        success_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        agent = self.agent_service.get(agent_id)
        if agent["status"] not in {"active", "running"}:
            raise PermissionError(f"agent status {agent['status']!r} cannot be run")
        active_run = self._start_run(
            agent=agent,
            parent_run_id=parent_run_id,
            workflow_run_id=workflow_run_id,
            workflow_node_run_id=workflow_node_run_id,
            budget=self._derive_workflow_budget(agent, workflow_run_id, budget),
            success_contract=success_contract,
        )
        instruction = self._build_instruction(agent)
        context = self.context_service.build(instruction)
        context["active_agent"] = agent
        started_clock = time.monotonic()

        try:
            reasoning = self.reasoner_service.plan(user_message=instruction, context=context)
            run_budget = dict(active_run["budget_snapshot"])
            max_tool_calls = run_budget.get("max_tool_calls")
            execution = self.executor_service.execute(
                plan=reasoning.plan,
                owner_approved=owner_approved,
                input_sources=["agent", "memory", "tasks", "owner"],
                max_tool_calls=int(max_tool_calls) if max_tool_calls is not None else None,
                allowed_tool_ids=list(agent["tools"]) if agent["tools"] else None,
            )
            reply = self.executor_service.compose_reply(
                reasoning.plan.reply,
                execution["executed_tools"],
                execution["blocked_tools"],
                reasoning.warning,
            )
            usage = dict(execution.get("usage") or {})
            usage["elapsed_seconds"] = max(0.0, time.monotonic() - started_clock)
            evaluation = self._evaluate_run(
                active_run=active_run,
                reply=reply,
                execution=execution,
                usage=usage,
            )
            outcome = "completed" if evaluation["passed"] else "failed"
            warning = reasoning.warning or ""
            if evaluation["reasons"]:
                contract_warning = "; ".join(evaluation["reasons"])
                warning = f"{warning}; {contract_warning}".strip("; ")
            record = self._finish_run(
                run_id=active_run["id"],
                reply=reply,
                outcome=outcome,
                reasoning_mode=reasoning.mode,
                model_name=reasoning.model_name or "",
                created_task_ids=execution["created_task_ids"],
                created_memory_ids=execution["created_memory_ids"],
                created_agent_ids=execution["created_agent_ids"],
                executed_tools=execution["executed_tools"],
                blocked_tools=execution["blocked_tools"],
                warning=warning,
                usage=usage,
                evaluation=evaluation,
            )
            self.audit_service.log(
                action_type="agent_run",
                action_tier=1,
                tool_name="agent_runner",
                outcome=outcome,
                approved_by_owner=owner_approved,
                input_sources=["agent", "memory", "tasks", "owner"],
                metadata={"agent_id": agent_id, "run_id": record["id"]},
            )
            return record
        except Exception as exc:  # noqa: BLE001
            record = self._finish_run(
                run_id=active_run["id"],
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
                usage={"elapsed_seconds": max(0.0, time.monotonic() - started_clock)},
                evaluation={
                    "passed": False,
                    "checks": {"execution": False},
                    "reasons": [str(exc)],
                },
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

    @staticmethod
    def _evaluate_run(
        *,
        active_run: dict[str, Any],
        reply: str,
        execution: dict[str, Any],
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        budget = dict(active_run.get("budget_snapshot") or {})
        contract = dict(active_run.get("success_contract_snapshot") or {})
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        time_budget_minutes = float(budget.get("time_budget_minutes", 30))
        checks["time_budget"] = usage.get("elapsed_seconds", 0.0) <= time_budget_minutes * 60
        if not checks["time_budget"]:
            reasons.append("agent exceeded its time budget")

        if budget.get("max_tool_calls") is not None:
            checks["max_tool_calls"] = int(usage.get("tool_calls", 0)) <= int(
                budget["max_tool_calls"]
            )
            if not checks["max_tool_calls"]:
                reasons.append("agent exceeded its tool-call budget")

        required_terms = [
            str(item).casefold()
            for item in contract.get("required_reply_terms", [])
            if str(item).strip()
        ]
        if required_terms:
            lowered_reply = reply.casefold()
            checks["required_reply_terms"] = all(
                term in lowered_reply for term in required_terms
            )
            if not checks["required_reply_terms"]:
                reasons.append("reply is missing required contract terms")

        forbidden_terms = [
            str(item).casefold()
            for item in contract.get("forbidden_reply_terms", [])
            if str(item).strip()
        ]
        if forbidden_terms:
            lowered_reply = reply.casefold()
            checks["forbidden_reply_terms"] = not any(
                term in lowered_reply for term in forbidden_terms
            )
            if not checks["forbidden_reply_terms"]:
                reasons.append("reply contains a forbidden contract term")

        executed_tool_ids = {
            str(item.get("tool_id", ""))
            for item in execution.get("executed_tools", [])
        }
        required_tool_ids = {
            str(item)
            for item in contract.get("required_tool_ids", [])
            if str(item).strip()
        }
        if required_tool_ids:
            checks["required_tool_ids"] = required_tool_ids.issubset(executed_tool_ids)
            if not checks["required_tool_ids"]:
                reasons.append("required tools were not executed")

        required_artifacts = [
            str(item).replace("\\", "/").casefold()
            for item in contract.get("required_artifacts", [])
            if str(item).strip()
        ]
        if required_artifacts:
            artifact_text = " ".join(
                AgentRunnerService._result_strings(
                    item.get("result", {})
                )
                for item in execution.get("executed_tools", [])
            ).replace("\\", "/").casefold()
            checks["required_artifacts"] = all(
                artifact in artifact_text for artifact in required_artifacts
            )
            if not checks["required_artifacts"]:
                reasons.append("required artifacts were not produced")

        if contract.get("min_score") is not None:
            score = (execution.get("evaluation") or {}).get("score")
            checks["min_score"] = (
                isinstance(score, (int, float))
                and float(score) >= float(contract["min_score"])
            )
            if not checks["min_score"]:
                reasons.append("run did not meet the minimum score contract")

        return {
            "passed": all(checks.values()) if checks else True,
            "checks": checks,
            "reasons": reasons,
        }

    @staticmethod
    def _result_strings(value: Any) -> str:
        if isinstance(value, dict):
            return " ".join(
                AgentRunnerService._result_strings(item)
                for item in value.values()
            )
        if isinstance(value, (list, tuple, set)):
            return " ".join(
                AgentRunnerService._result_strings(item)
                for item in value
            )
        return str(value)

    def list_runs(self, agent_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM agent_runs
                WHERE agent_id = ?
                ORDER BY COALESCE(started_at, created_at) DESC, rowid DESC
                LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._row_to_dict(row)

    def fail_run_for_workflow_node(
        self, workflow_node_run_id: str, *, status: str = "failed", error: str = ""
    ) -> int:
        """Finalize a still-running agent_run whose owning workflow node was
        force-terminated (timeout/cancel/shutdown) so it is never orphaned 'running'."""
        now = utc_now()
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET status = ?, outcome = ?, warning = ?, completed_at = ?, updated_at = ?
                WHERE workflow_node_run_id = ? AND status = 'running'
                """,
                (status, status, str(error)[:2000], now, now, workflow_node_run_id),
            )
            return int(cursor.rowcount)

    def list_running_workflow_node_runs(self) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, workflow_node_run_id
                FROM agent_runs
                WHERE status = 'running' AND workflow_node_run_id IS NOT NULL
                """
            ).fetchall()
        return [
            {"id": row["id"], "workflow_node_run_id": row["workflow_node_run_id"]}
            for row in rows
        ]

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
        agent = self.agent_service.get(agent_id)
        now = utc_now()
        return self._insert_run(
            run_id=self.db.make_id("agentrun"),
            agent=agent,
            goal=goal,
            reply=reply,
            outcome=outcome,
            status=outcome,
            reasoning_mode=reasoning_mode,
            model_name=model_name,
            created_task_ids=created_task_ids,
            created_memory_ids=created_memory_ids,
            created_agent_ids=created_agent_ids,
            executed_tools=executed_tools,
            blocked_tools=blocked_tools,
            warning=warning,
            parent_run_id=None,
            workflow_run_id=None,
            workflow_node_run_id=None,
            budget=agent["budget"],
            success_contract=agent["success_contract"],
            usage={},
            evaluation={},
            started_at=now,
            completed_at=now,
        )

    def _derive_workflow_budget(
        self,
        agent: dict[str, Any],
        workflow_run_id: str | None,
        budget: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """PRD §7 supervisor budget: agent runs within ONE workflow share the
        agent's tool-call budget so they cannot collectively exceed it. Applies
        only when this run is part of a workflow, no explicit budget was passed,
        and the agent declares a finite max_tool_calls. Standalone or unlimited
        runs are returned unchanged (backward compatible).
        """
        if budget is not None or not workflow_run_id:
            return budget
        agent_budget = dict(agent.get("budget") or {})
        cap = agent_budget.get("max_tool_calls")
        if not isinstance(cap, int) or isinstance(cap, bool):
            return budget  # unlimited / unset -> nothing to ration
        consumed = self._workflow_consumed_tool_calls(workflow_run_id)
        agent_budget["max_tool_calls"] = max(0, cap - consumed)
        return agent_budget

    def _workflow_consumed_tool_calls(self, workflow_run_id: str) -> int:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT usage_json FROM agent_runs WHERE workflow_run_id = ?",
                (workflow_run_id,),
            ).fetchall()
        total = 0
        for row in rows:
            try:
                usage = self.db.loads(row["usage_json"]) or {}
            except Exception:  # noqa: BLE001
                usage = {}
            total += int(usage.get("tool_calls") or 0)
        return total

    def _start_run(
        self,
        *,
        agent: dict[str, Any],
        parent_run_id: str | None,
        workflow_run_id: str | None,
        workflow_node_run_id: str | None,
        budget: dict[str, Any] | None,
        success_contract: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return self._insert_run(
            run_id=self.db.make_id("agentrun"),
            agent=agent,
            goal=agent["goal"],
            reply="",
            outcome="running",
            status="running",
            reasoning_mode="",
            model_name="",
            created_task_ids=[],
            created_memory_ids=[],
            created_agent_ids=[],
            executed_tools=[],
            blocked_tools=[],
            warning="",
            parent_run_id=parent_run_id,
            workflow_run_id=workflow_run_id,
            workflow_node_run_id=workflow_node_run_id,
            budget=dict(budget if budget is not None else agent["budget"]),
            success_contract=dict(
                success_contract
                if success_contract is not None
                else agent["success_contract"]
            ),
            usage={},
            evaluation={},
            started_at=utc_now(),
            completed_at=None,
        )

    def _finish_run(
        self,
        *,
        run_id: str,
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
        usage: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> dict[str, Any]:
        completed_at = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE agent_runs
                SET reply = ?, outcome = ?, status = ?, reasoning_mode = ?,
                    model_name = ?, created_task_ids_json = ?,
                    created_memory_ids_json = ?, created_agent_ids_json = ?,
                    executed_tools_json = ?, blocked_tools_json = ?, warning = ?,
                    usage_json = ?, evaluation_json = ?, completed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    reply,
                    outcome,
                    outcome,
                    reasoning_mode,
                    model_name,
                    self.db.dumps(created_task_ids),
                    self.db.dumps(created_memory_ids),
                    self.db.dumps(created_agent_ids),
                    self.db.dumps(executed_tools),
                    self.db.dumps(blocked_tools),
                    warning,
                    self.db.dumps(usage),
                    self.db.dumps(evaluation),
                    completed_at,
                    completed_at,
                    run_id,
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise KeyError(run_id)
        return self.get_run(run_id)

    def _insert_run(
        self,
        *,
        run_id: str,
        agent: dict[str, Any],
        goal: str,
        reply: str,
        outcome: str,
        status: str,
        reasoning_mode: str,
        model_name: str,
        created_task_ids: list[str],
        created_memory_ids: list[str],
        created_agent_ids: list[str],
        executed_tools: list[dict[str, Any]],
        blocked_tools: list[dict[str, Any]],
        warning: str,
        parent_run_id: str | None,
        workflow_run_id: str | None,
        workflow_node_run_id: str | None,
        budget: dict[str, Any],
        success_contract: dict[str, Any],
        usage: dict[str, Any],
        evaluation: dict[str, Any],
        started_at: str,
        completed_at: str | None,
    ) -> dict[str, Any]:
        definition_snapshot = self._definition_snapshot(agent)
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs (
                    id, agent_id, goal, reply, outcome, reasoning_mode, model_name,
                    created_task_ids_json, created_memory_ids_json,
                    created_agent_ids_json, executed_tools_json,
                    blocked_tools_json, warning, created_at, status,
                    definition_id, definition_version, definition_version_id,
                    definition_snapshot_json, parent_agent_id, root_agent_id,
                    depth, parent_run_id, workflow_run_id, workflow_node_run_id,
                    budget_snapshot_json, success_contract_snapshot_json,
                    usage_json, evaluation_json, started_at, completed_at,
                    updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    run_id,
                    agent["id"],
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
                    started_at,
                    status,
                    agent["definition_id"],
                    agent["definition_version"],
                    agent["definition_version_id"],
                    self.db.dumps(definition_snapshot),
                    agent["parent_agent_id"],
                    agent["root_agent_id"],
                    agent["depth"],
                    parent_run_id,
                    workflow_run_id,
                    workflow_node_run_id,
                    self.db.dumps(budget),
                    self.db.dumps(success_contract),
                    self.db.dumps(usage),
                    self.db.dumps(evaluation),
                    started_at,
                    completed_at,
                    completed_at or started_at,
                ),
            )
        return self.get_run(run_id)

    def _definition_snapshot(self, agent: dict[str, Any]) -> dict[str, Any]:
        try:
            snapshot = self.agent_service.get_definition_version(
                agent["definition_id"],
                agent["definition_version"],
            )
        except (AttributeError, KeyError):
            snapshot = {
                "id": agent.get("definition_version_id"),
                "definition_id": agent.get("definition_id"),
                "version": agent.get("definition_version"),
                "name": agent["name"],
                "agent_type": agent["agent_type"],
                "goal": agent["goal"],
                "tools": agent["tools"],
                "memory_scope": agent["memory_scope"],
                "time_budget_minutes": agent["time_budget_minutes"],
                "notes": agent["notes"],
                "budget": agent["budget"],
                "success_contract": agent["success_contract"],
            }
        snapshot["definition_version_id"] = snapshot.get("id")
        snapshot["definition_version"] = snapshot.get("version")
        return snapshot

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
        keys = set(row.keys())
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
            "status": row["status"] if "status" in keys else row["outcome"],
            "definition_id": (
                row["definition_id"] if "definition_id" in keys else None
            ),
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
            "definition_snapshot": self.db.loads(
                row["definition_snapshot_json"]
                if "definition_snapshot_json" in keys
                else "{}"
            ),
            "parent_agent_id": (
                row["parent_agent_id"] if "parent_agent_id" in keys else None
            ),
            "root_agent_id": (
                row["root_agent_id"] if "root_agent_id" in keys else None
            ),
            "depth": row["depth"] if "depth" in keys else 0,
            "parent_run_id": (
                row["parent_run_id"] if "parent_run_id" in keys else None
            ),
            "workflow_run_id": (
                row["workflow_run_id"] if "workflow_run_id" in keys else None
            ),
            "workflow_node_run_id": (
                row["workflow_node_run_id"]
                if "workflow_node_run_id" in keys
                else None
            ),
            "budget_snapshot": self.db.loads(
                row["budget_snapshot_json"]
                if "budget_snapshot_json" in keys
                else "{}"
            ),
            "success_contract_snapshot": self.db.loads(
                row["success_contract_snapshot_json"]
                if "success_contract_snapshot_json" in keys
                else "{}"
            ),
            "usage": self.db.loads(
                row["usage_json"] if "usage_json" in keys else "{}"
            ),
            "evaluation": self.db.loads(
                row["evaluation_json"]
                if "evaluation_json" in keys
                else "{}"
            ),
            "started_at": (
                row["started_at"]
                if "started_at" in keys and row["started_at"]
                else row["created_at"]
            ),
            "completed_at": (
                row["completed_at"] if "completed_at" in keys else row["created_at"]
            ),
            "updated_at": (
                row["updated_at"]
                if "updated_at" in keys and row["updated_at"]
                else row["created_at"]
            ),
            "created_at": row["created_at"],
        }
