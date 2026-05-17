from __future__ import annotations

import time
from typing import Any

from project_q.models import RoutineUpdate, utc_now


class RoutineRunnerService:
    def __init__(self, db, routine_service, agent_runner, tool_registry, policy_service, audit_service) -> None:
        self.db = db
        self.routine_service = routine_service
        self.agent_runner = agent_runner
        self.tool_registry = tool_registry
        self.policy_service = policy_service
        self.audit_service = audit_service

    def run(self, routine_id: str, *, owner_approved: bool) -> dict[str, Any]:
        routine = self.routine_service.get(routine_id)
        self.routine_service.update(routine_id, RoutineUpdate(status="running"))
        step_results: list[dict[str, Any]] = []
        blocked_steps: list[dict[str, Any]] = []
        warning = ""
        outcome = "completed"

        try:
            for index, step in enumerate(routine["steps"], start=1):
                label = step.get("label") or f"Step {index}"
                try:
                    result = self._execute_step(
                        routine=routine,
                        step=step,
                        index=index,
                        label=label,
                        owner_approved=owner_approved,
                    )
                except Exception as exc:  # noqa: BLE001
                    result = {
                        "index": index,
                        "label": label,
                        "step_type": step["step_type"],
                        "status": "failed",
                        "error": str(exc),
                    }
                if result["status"] == "completed":
                    step_results.append(result)
                    continue

                blocked_steps.append(result)
                if not step.get("continue_on_error", False):
                    outcome = result["status"]
                    warning = result.get("reason") or result.get("error") or warning
                    break

            if outcome == "completed" and blocked_steps:
                outcome = "completed_with_blocks"

            reply = self._compose_reply(routine, outcome, step_results, blocked_steps, warning)
            record = self._store_run(
                routine_id=routine_id,
                goal=routine["goal"],
                reply=reply,
                outcome=outcome,
                step_results=step_results,
                blocked_steps=blocked_steps,
                warning=warning,
            )
            self.routine_service.update(
                routine_id,
                RoutineUpdate(status="blocked" if outcome in {"blocked", "failed"} else "active"),
            )
            self.audit_service.log(
                action_type="routine_run",
                action_tier=1,
                tool_name="routine_runner",
                outcome=outcome,
                approved_by_owner=owner_approved,
                input_sources=["owner", "routines", "agents", "tools"],
                metadata={"routine_id": routine_id, "run_id": record["id"]},
                error=warning,
            )
            return record
        except Exception as exc:  # noqa: BLE001
            self.routine_service.update(routine_id, RoutineUpdate(status="blocked"))
            record = self._store_run(
                routine_id=routine_id,
                goal=routine["goal"],
                reply="",
                outcome="failed",
                step_results=step_results,
                blocked_steps=blocked_steps,
                warning=str(exc),
            )
            self.audit_service.log(
                action_type="routine_run",
                action_tier=1,
                tool_name="routine_runner",
                outcome="failed",
                approved_by_owner=owner_approved,
                input_sources=["owner", "routines", "agents", "tools"],
                metadata={"routine_id": routine_id, "run_id": record["id"]},
                error=str(exc),
            )
            raise

    def list_runs(self, routine_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM routine_runs
                WHERE routine_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (routine_id, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def _execute_step(
        self,
        *,
        routine: dict[str, Any],
        step: dict[str, Any],
        index: int,
        label: str,
        owner_approved: bool,
    ) -> dict[str, Any]:
        if step.get("requires_owner_approval") and not owner_approved:
            return {
                "index": index,
                "label": label,
                "step_type": step["step_type"],
                "status": "blocked",
                "reason": "step requires explicit owner approval",
            }

        if step["step_type"] == "delay":
            delay_seconds = min(int(step["delay_seconds"]), 60)
            time.sleep(delay_seconds)
            return {
                "index": index,
                "label": label,
                "step_type": "delay",
                "status": "completed",
                "result": {"delay_seconds": delay_seconds},
            }

        if step["step_type"] == "agent":
            agent_run = self.agent_runner.run(
                step["agent_id"],
                owner_approved=owner_approved or bool(routine["trusted"]),
            )
            return {
                "index": index,
                "label": label,
                "step_type": "agent",
                "status": "completed",
                "agent_id": step["agent_id"],
                "result": agent_run,
            }

        tool = self.tool_registry.get(step["tool_id"])
        decision = self.policy_service.authorize_tool(
            tier=tool.definition.tier,
            owner_approved=owner_approved,
            trusted_routine=bool(routine["trusted"]),
        )
        if not decision.allowed:
            self.audit_service.log(
                action_type="routine_step",
                action_tier=tool.definition.tier,
                tool_name=step["tool_id"],
                outcome="blocked",
                input_sources=["owner", "routines"],
                metadata={
                    "routine_id": routine["id"],
                    "step_index": index,
                    "label": label,
                    "reason": decision.reason,
                },
            )
            return {
                "index": index,
                "label": label,
                "step_type": "tool",
                "tool_id": step["tool_id"],
                "status": "blocked",
                "reason": decision.reason,
            }

        result = tool.execute(step.get("payload", {}))
        self.audit_service.log(
            action_type="routine_step",
            action_tier=tool.definition.tier,
            tool_name=step["tool_id"],
            outcome="completed",
            approved_by_owner=owner_approved,
            input_sources=["owner", "routines"],
            metadata={
                "routine_id": routine["id"],
                "step_index": index,
                "label": label,
                "payload": step.get("payload", {}),
            },
        )
        return {
            "index": index,
            "label": label,
            "step_type": "tool",
            "tool_id": step["tool_id"],
            "status": "completed",
            "result": result,
        }

    def _compose_reply(
        self,
        routine: dict[str, Any],
        outcome: str,
        step_results: list[dict[str, Any]],
        blocked_steps: list[dict[str, Any]],
        warning: str,
    ) -> str:
        sections = [
            f"Routine `{routine['name']}` finished with outcome: {outcome}.",
        ]
        if step_results:
            completed_lines = []
            for item in step_results:
                preview = item.get("result", {})
                preview_text = preview.get("title") or preview.get("path") or str(preview)
                completed_lines.append(f"- {item['label']}: {preview_text[:220]}")
            sections.append("Completed steps:\n" + "\n".join(completed_lines))
        if blocked_steps:
            blocked_lines = []
            for item in blocked_steps:
                reason = item.get("reason") or item.get("error") or "blocked"
                blocked_lines.append(f"- {item['label']}: {reason}")
            sections.append("Blocked steps:\n" + "\n".join(blocked_lines))
        if warning:
            sections.append(f"Warning: {warning}")
        return "\n\n".join(section for section in sections if section)

    def _store_run(
        self,
        *,
        routine_id: str,
        goal: str,
        reply: str,
        outcome: str,
        step_results: list[dict[str, Any]],
        blocked_steps: list[dict[str, Any]],
        warning: str,
    ) -> dict[str, Any]:
        run_id = self.db.make_id("routinrun")
        created_at = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO routine_runs (
                    id, routine_id, goal, reply, outcome, step_results_json,
                    blocked_steps_json, warning, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    routine_id,
                    goal,
                    reply,
                    outcome,
                    self.db.dumps(step_results),
                    self.db.dumps(blocked_steps),
                    warning,
                    created_at,
                ),
            )
        return self.list_runs(routine_id, limit=1)[0]

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "routine_id": row["routine_id"],
            "goal": row["goal"],
            "reply": row["reply"],
            "outcome": row["outcome"],
            "step_results": self.db.loads(row["step_results_json"]),
            "blocked_steps": self.db.loads(row["blocked_steps_json"]),
            "warning": row["warning"] or None,
            "created_at": row["created_at"],
        }
