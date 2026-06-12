from __future__ import annotations

from pathlib import Path
from typing import Any

from project_q.models import AgentCreate, MemoryCreate, ReasonerPlan, RoutineCreate, TaskCreate
from project_q.services.sync import SyncEventService


class PlanExecutorService:
    def __init__(
        self,
        memory_service,
        task_service,
        agent_service,
        routine_service,
        tool_registry,
        policy_service,
        audit_service,
        artifact_service,
        code_repair_service,
        approval_service=None,
    ) -> None:
        self.memory_service = memory_service
        self.task_service = task_service
        self.agent_service = agent_service
        self.routine_service = routine_service
        self.tool_registry = tool_registry
        self.policy_service = policy_service
        self.audit_service = audit_service
        self.artifact_service = artifact_service
        self.code_repair_service = code_repair_service
        self.approval_service = approval_service

    def execute(
        self,
        *,
        plan: ReasonerPlan,
        owner_approved: bool,
        input_sources: list[str],
        session_id: str = "",
        originating_goal: str = "",
        model: str = "",
        plan_id: str = "",
    ) -> dict[str, Any]:
        created_task_ids: list[str] = []
        created_memory_ids: list[str] = []
        created_agent_ids: list[str] = []
        created_routine_ids: list[str] = []
        executed_tools: list[dict[str, Any]] = []
        blocked_tools: list[dict[str, Any]] = []
        created_action_request_ids: list[str] = []

        for memory_plan in plan.memory_writes:
            memory = self.memory_service.create(
                MemoryCreate(
                    text=memory_plan.text,
                    kind=memory_plan.kind,
                    source="reasoner",
                    confidence=memory_plan.confidence,
                    owner_confirmed=False,
                )
            )
            created_memory_ids.append(memory["id"])

        for task_plan in plan.task_writes:
            task = self.task_service.create(
                TaskCreate(
                    title=task_plan.title,
                    description=task_plan.description,
                    priority=task_plan.priority,
                    source="reasoner",
                )
            )
            created_task_ids.append(task["id"])

        for agent_plan in plan.agent_writes:
            agent = self.agent_service.create(
                AgentCreate(
                    name=agent_plan.name,
                    agent_type=agent_plan.agent_type,
                    goal=agent_plan.goal,
                    status=agent_plan.status,
                    tools=agent_plan.tools,
                )
            )
            created_agent_ids.append(agent["id"])

        for routine_plan in plan.routine_writes:
            routine = self.routine_service.create(
                RoutineCreate(
                    name=routine_plan.name,
                    goal=routine_plan.goal,
                    description=routine_plan.description,
                    status=routine_plan.status,
                    trigger_type=routine_plan.trigger_type,
                    trusted=routine_plan.trusted,
                    tools=routine_plan.tools,
                    steps=routine_plan.steps,
                    notes=routine_plan.notes,
                )
            )
            created_routine_ids.append(routine["id"])

        for tool_call in plan.tool_calls[:3]:
            tool = self.tool_registry.get(tool_call.tool_id)
            decision = self.policy_service.authorize_tool(
                tier=tool.definition.tier,
                owner_approved=owner_approved,
                input_sources=input_sources,
                tool_id=tool_call.tool_id,
            )
            if not decision.allowed:
                blocked = {
                    "tool_id": tool_call.tool_id,
                    "reason": decision.reason,
                    "payload": SyncEventService._redact(tool_call.payload),
                }
                if (
                    self.approval_service is not None
                    and tool.definition.tier in {2, 3}
                    and decision.reason == f"tier {tool.definition.tier} requires owner approval"
                ):
                    action_request = self.approval_service.create_request(
                        tool_id=tool_call.tool_id,
                        action_tier=tool.definition.tier,
                        payload=tool_call.payload,
                        summary=tool_call.reason or f"Run {tool.definition.name}",
                        session_id=session_id,
                        originating_goal=originating_goal or plan.reply,
                        input_sources=input_sources,
                        model=model,
                        plan_id=plan_id,
                    )
                    blocked["action_request_id"] = action_request["id"]
                    blocked["payload"] = action_request["redacted_preview"]
                    created_action_request_ids.append(action_request["id"])
                blocked_tools.append(blocked)
                self.audit_service.log(
                    action_type="tool_execution",
                    action_tier=tool.definition.tier,
                    tool_name=tool_call.tool_id,
                    outcome="blocked",
                    input_sources=input_sources,
                    metadata={
                        "reason": decision.reason,
                        "payload": SyncEventService._redact(tool_call.payload),
                        "action_request_id": blocked.get("action_request_id", ""),
                    },
                )
                continue

            result = tool.execute(tool_call.payload)
            validation = self._validate_artifact(tool_call.tool_id, result)
            if validation is not None:
                validation = self._repair_artifact_if_needed(tool_call.tool_id, result, validation)
                result["validation"] = validation
                self.audit_service.log(
                    action_type="artifact_validation",
                    action_tier=0,
                    tool_name=tool_call.tool_id,
                    outcome=validation["status"],
                    input_sources=input_sources,
                    metadata={"path": result.get("path"), "validation": validation},
                    error=validation.get("message", "") if validation["status"] == "invalid" else "",
                )
            executed_tools.append(
                {
                    "tool_id": tool_call.tool_id,
                    "reason": tool_call.reason,
                    "payload": tool_call.payload,
                    "result": result,
                }
            )
            self.audit_service.log(
                action_type="tool_execution",
                action_tier=tool.definition.tier,
                tool_name=tool_call.tool_id,
                outcome="completed",
                approved_by_owner=owner_approved,
                input_sources=input_sources,
                metadata={"payload": tool_call.payload, "reason": tool_call.reason},
            )

        return {
            "created_task_ids": created_task_ids,
            "created_memory_ids": created_memory_ids,
            "created_agent_ids": created_agent_ids,
            "created_routine_ids": created_routine_ids,
            "executed_tools": executed_tools,
            "blocked_tools": blocked_tools,
            "created_action_request_ids": created_action_request_ids,
        }

    @staticmethod
    def compose_reply(
        base_reply: str,
        executed_tools: list[dict[str, Any]],
        blocked_tools: list[dict[str, Any]],
        warning: str | None,
    ) -> str:
        sections = [base_reply]

        if executed_tools:
            tool_lines = []
            validation_lines = []
            for item in executed_tools:
                result = item["result"]
                if item["tool_id"] == "filesystem.resolve_file_request":
                    preview = PlanExecutorService._format_file_resolution_result(result)
                    preview_limit = 1400
                elif item["tool_id"] == "filesystem.open_file_choice":
                    selected = result.get("selected", {})
                    preview = f"{result.get('mode', 'reveal')}ed {selected.get('name', selected.get('path', 'selected file'))}"
                    preview_limit = 220
                elif item["tool_id"] == "knowledge.answer":
                    preview = result.get("answer") or str(result)
                    preview_limit = 3500
                elif item["tool_id"] == "project.plan_build":
                    preview = (
                        f"{result.get('status', 'planned')} {result.get('task_type', 'project')} "
                        f"dispatch for {result.get('project_name', 'Project Q build')} at "
                        f"{result.get('project_path', 'workspace')}"
                    )
                    preview_limit = 520
                elif item["tool_id"] == "code.generate_website":
                    preview = f"{result.get('summary', 'Website generated')} Entry: {result.get('entrypoint', '')}"
                    preview_limit = 520
                elif item["tool_id"] == "code.generate_project":
                    preview = f"{result.get('summary', 'Project generated')} Run: {result.get('how_to_run', '')}"
                    preview_limit = 520
                elif item["tool_id"] == "training.prepare_lora_job":
                    preview = (
                        f"{result.get('summary', 'LoRA job ready')} "
                        f"Script: {result.get('train_script_path', '')}"
                    )
                    preview_limit = 720
                elif item["tool_id"] == "training.capability_plan":
                    preview = (
                        f"{result.get('summary', 'Capability plan ready')} "
                        f"Next: {', '.join(result.get('execution_order', [])[:2])}"
                    )
                    preview_limit = 900
                else:
                    preview = result.get("title") or result.get("text_excerpt") or result.get("path") or str(result)
                    preview_limit = 220
                tool_lines.append(f"- {item['tool_id']}: {preview[:preview_limit]}")
                validation = result.get("validation")
                if isinstance(validation, dict) and validation.get("status") not in {None, "ok", "skipped"}:
                    validation_lines.append(
                        f"- {item['tool_id']}: {validation.get('message', 'artifact validation issue')}"
                    )
            sections.append("Executed tools:\n" + "\n".join(tool_lines))
            if validation_lines:
                sections.append("Validation warnings:\n" + "\n".join(validation_lines))

        if blocked_tools:
            block_lines = [f"- {item['tool_id']}: {item['reason']}" for item in blocked_tools]
            sections.append("Blocked pending approval:\n" + "\n".join(block_lines))

        if warning:
            sections.append(f"Provider warning: {warning}")

        return "\n\n".join(section for section in sections if section)

    @staticmethod
    def _format_file_resolution_result(result: dict[str, Any]) -> str:
        status = result.get("status")
        if status == "needs_confirmation":
            lines = [result.get("message", "Which file did you mean?")]
            choice_id = result.get("choice_id")
            if choice_id:
                lines.append(f"Choice ID: {choice_id}")
            for index, option in enumerate(result.get("options", [])[:5], start=1):
                preview = option.get("preview") or option.get("path", "")
                lines.append(f"{index}. {option.get('name')} - {option.get('path')} - {preview[:120]}")
            lines.append("Reply with `open option 1` or `reveal option 1`.")
            return "\n".join(lines)
        if status == "resolved":
            selected = result.get("selected", {})
            return f"{result.get('action', 'reveal')}ed {selected.get('name', selected.get('path', 'selected file'))}"
        return result.get("message") or str(result)

    def _validate_artifact(self, tool_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
        if tool_id != "filesystem.write_file":
            return None
        path = result.get("path")
        if not isinstance(path, str) or not path:
            return None
        return self.artifact_service.validate_written_file(path)

    def _repair_artifact_if_needed(
        self,
        tool_id: str,
        result: dict[str, Any],
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_id != "filesystem.write_file":
            return validation
        if validation.get("status") != "invalid":
            runtime_validation = self._smoke_test_python_artifact(result, validation)
            if runtime_validation.get("status") != "runtime_invalid":
                return validation
            validation = runtime_validation
        path_value = result.get("path")
        if not isinstance(path_value, str) or not path_value.lower().endswith(".py"):
            return validation

        path = Path(path_value)
        original = path.read_text(encoding="utf-8")
        repair = self.code_repair_service.repair_python_file(path=path, content=original, validation=validation)
        if repair is None:
            return validation

        path.write_text(repair["content"], encoding="utf-8")
        repaired_validation = self.artifact_service.validate_written_file(path)
        if repaired_validation.get("status") not in {"ok", "repaired"}:
            path.write_text(original, encoding="utf-8")
            return validation

        runtime_validation = self._smoke_test_python_artifact(result, repaired_validation)
        if runtime_validation.get("status") == "runtime_invalid":
            path.write_text(original, encoding="utf-8")
            return validation

        repaired_validation["status"] = "repaired_with_model"
        if validation.get("status") == "runtime_invalid":
            repaired_validation["message"] = (
                "Repaired generated Python after a runtime smoke test failure and verified it successfully."
            )
        else:
            repaired_validation["message"] = (
                "Repaired generated Python with the coding model and verified syntax successfully."
            )
        repaired_validation["model_name"] = repair["model_name"]
        if runtime_validation.get("status") == "runtime_ok":
            repaired_validation["runtime_stdout"] = runtime_validation.get("stdout", "")
        return repaired_validation

    def _smoke_test_python_artifact(
        self,
        result: dict[str, Any],
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        path_value = result.get("path")
        if not isinstance(path_value, str) or not path_value.lower().endswith(".py"):
            return validation
        runtime_validation = self.artifact_service.smoke_test_written_file(path_value)
        return runtime_validation if runtime_validation.get("status") != "skipped" else validation
