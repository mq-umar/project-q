from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    owner_approved: bool = False


class ChatResponse(BaseModel):
    reply: str
    created_task_ids: list[str] = Field(default_factory=list)
    created_memory_ids: list[str] = Field(default_factory=list)
    created_agent_ids: list[str] = Field(default_factory=list)
    created_routine_ids: list[str] = Field(default_factory=list)
    executed_tools: list[dict[str, Any]] = Field(default_factory=list)
    blocked_tools: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_mode: str = "local"
    model_name: str | None = None
    context_summary: dict[str, Any] = Field(default_factory=dict)


class MemoryCreate(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    kind: str = "semantic"
    source: str = "owner"
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    owner_confirmed: bool = True
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryUpdate(BaseModel):
    text: str | None = None
    kind: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    owner_confirmed: bool | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    priority: int = Field(default=3, ge=1, le=5)
    status: str = "pending"
    due_at: str | None = None
    source: str = "owner"
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    status: str | None = None
    due_at: str | None = None
    metadata: dict[str, Any] | None = None


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    agent_type: str = "general"
    goal: str = Field(min_length=1, max_length=4000)
    status: str = "draft"
    tools: list[str] = Field(default_factory=list)
    memory_scope: str = "task-local"
    time_budget_minutes: int = Field(default=30, ge=1, le=1440)
    notes: str = ""


class AgentUpdate(BaseModel):
    name: str | None = None
    agent_type: str | None = None
    goal: str | None = None
    status: str | None = None
    tools: list[str] | None = None
    memory_scope: str | None = None
    time_budget_minutes: int | None = Field(default=None, ge=1, le=1440)
    notes: str | None = None


class RoutineStepDefinition(BaseModel):
    step_type: Literal["tool", "agent", "delay"] = "tool"
    label: str = ""
    tool_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None
    delay_seconds: int | None = Field(default=None, ge=1, le=600)
    continue_on_error: bool = False
    requires_owner_approval: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_step_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        step_type = str(value.get("step_type", "")).strip().lower()
        if step_type not in {"tool", "agent", "delay"}:
            if value.get("agent_id"):
                value["step_type"] = "agent"
            elif value.get("tool_id") or value.get("payload"):
                value["step_type"] = "tool"
            elif step_type in {"wait", "sleep", "pause"}:
                value["step_type"] = "delay"
            else:
                value["step_type"] = "tool"
        return value

    @field_validator("delay_seconds", mode="before")
    @classmethod
    def normalize_delay_seconds(cls, value: Any) -> Any:
        if value in {None, "", 0, "0"}:
            return None
        return value

    @model_validator(mode="after")
    def validate_step(self) -> "RoutineStepDefinition":
        if self.step_type == "tool" and not self.tool_id:
            raise ValueError("tool steps require tool_id")
        if self.step_type == "agent" and not self.agent_id:
            raise ValueError("agent steps require agent_id")
        if self.step_type == "delay" and self.delay_seconds is None:
            raise ValueError("delay steps require delay_seconds")
        return self


class RoutineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=4000)
    description: str = ""
    status: str = "active"
    trigger_type: str = "manual"
    trusted: bool = False
    tools: list[str] = Field(default_factory=list)
    steps: list[RoutineStepDefinition] = Field(default_factory=list)
    notes: str = ""


class RoutineUpdate(BaseModel):
    name: str | None = None
    goal: str | None = None
    description: str | None = None
    status: str | None = None
    trigger_type: str | None = None
    trusted: bool | None = None
    tools: list[str] | None = None
    steps: list[RoutineStepDefinition] | None = None
    notes: str | None = None


class SettingsUpdate(BaseModel):
    owner_name: str | None = None
    aggression_level: str | None = None
    auto_approve_tier: int | None = Field(default=None, ge=0, le=3)
    memory_mode: str | None = None
    voice_enabled: bool | None = None
    sync_enabled: bool | None = None
    notifications_enabled: bool | None = None
    provider_enabled: bool | None = None
    provider_type: str | None = None
    model_name: str | None = None
    model_base_url: str | None = None
    model_secret_name: str | None = None
    ollama_model_routing_enabled: bool | None = None
    ollama_general_model: str | None = None
    ollama_coding_model: str | None = None
    ollama_reasoning_model: str | None = None
    ollama_fast_model: str | None = None
    provider_timeout_seconds: int | None = Field(default=None, ge=5, le=300)
    browser_headless: bool | None = None
    browser_channel: str | None = None
    browser_executable_path: str | None = None
    file_access_roots: list[str] | None = None
    learning_enabled: bool | None = None
    learning_interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    learning_max_cycles_per_start: int | None = Field(default=None, ge=1, le=1000)

    @field_validator("file_access_roots", mode="before")
    @classmethod
    def normalize_file_access_roots(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, str):
            candidates = value.replace("\r", "\n").split("\n")
        else:
            candidates = value
        normalized = []
        seen = set()
        for item in candidates:
            text = str(item).strip()
            if not text or text.lower() in seen:
                continue
            normalized.append(text)
            seen.add(text.lower())
        return normalized


class LearningStartRequest(BaseModel):
    max_cycles: int | None = Field(default=None, ge=1, le=1000)
    interval_seconds: int | None = Field(default=None, ge=1, le=86400)


class LearningRunOnceRequest(BaseModel):
    reason: str = Field(default="manual", max_length=500)


class AgentRunRequest(BaseModel):
    owner_approved: bool = False


class RoutineRunRequest(BaseModel):
    owner_approved: bool = False


class ToolExecutionRequest(BaseModel):
    tool_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    owner_approved: bool = False


class SecretUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=5000)
    description: str = ""


class ToolCallPlan(BaseModel):
    tool_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        payload = value.get("payload")
        tool_id = str(value.get("tool_id", ""))
        if payload is None:
            value["payload"] = {}
            return value

        if isinstance(payload, str):
            normalized = payload.strip()
            if not normalized:
                value["payload"] = {}
                return value
            try:
                parsed = json.loads(normalized)
                if isinstance(parsed, dict):
                    value["payload"] = parsed
                    return value
            except json.JSONDecodeError:
                pass

            if tool_id == "shell.run_command":
                value["payload"] = {"command": normalized, "workdir": ".", "timeout_seconds": 30}
            elif tool_id in {"filesystem.list_directory", "filesystem.read_file"}:
                value["payload"] = {"path": normalized}
            elif tool_id == "filesystem.search_files":
                value["payload"] = {"root": ".", "query": normalized, "include_content": True}
            elif tool_id == "filesystem.resolve_file_request":
                value["payload"] = {"root": ".", "instruction": normalized, "query": normalized, "action": "reveal"}
            elif tool_id == "filesystem.open_file_choice":
                value["payload"] = {"selection": int(normalized) if normalized.isdigit() else 1, "mode": "reveal"}
            elif tool_id in {"spreadsheet.inspect", "spreadsheet.analyze", "spreadsheet.write_analysis"}:
                value["payload"] = {"path": normalized}
            elif tool_id == "diagnostics.run_self_check":
                value["payload"] = {"source": normalized or "model"}
            elif tool_id == "diagnostics.auto_repair":
                value["payload"] = {"source": normalized or "model"}
            elif tool_id == "training.capability_plan":
                value["payload"] = {}
            elif tool_id == "training.export_dataset":
                value["payload"] = {"reason": normalized or "model"}
            elif tool_id == "training.prepare_lora_job":
                value["payload"] = {"reason": normalized or "model"}
            elif tool_id == "knowledge.answer":
                value["payload"] = {"instruction": normalized}
            elif tool_id == "code.generate_website":
                value["payload"] = {"instruction": normalized}
            elif tool_id == "code.generate_project":
                value["payload"] = {"instruction": normalized}
            elif tool_id == "project.plan_build":
                value["payload"] = {"instruction": normalized}
            elif tool_id == "browser.inspect_page":
                value["payload"] = {"url": normalized, "include_links": True}
            elif tool_id == "windows.activate_window":
                value["payload"] = {"window_title": normalized}
            elif tool_id == "windows.send_keys":
                value["payload"] = {"keys": normalized}
            else:
                value["payload"] = {"value": normalized}
        return value


class ReasonerMemoryPlan(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    kind: str = "semantic"
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class ReasonerTaskPlan(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    priority: int = Field(default=3, ge=1, le=5)

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, value: Any) -> Any:
        if isinstance(value, str):
            lowered = value.strip().lower()
            mapping = {
                "critical": 1,
                "urgent": 1,
                "highest": 1,
                "high": 1,
                "medium": 3,
                "normal": 3,
                "default": 3,
                "low": 5,
                "lowest": 5,
            }
            if lowered in mapping:
                return mapping[lowered]
            if lowered.isdigit():
                return int(lowered)
        return value


class ReasonerAgentPlan(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    agent_type: str = "general"
    goal: str = Field(min_length=1, max_length=4000)
    status: str = "active"
    tools: list[str] = Field(default_factory=list)


class ReasonerRoutinePlan(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=4000)
    description: str = ""
    status: str = "active"
    trigger_type: str = "manual"
    trusted: bool = False
    tools: list[str] = Field(default_factory=list)
    steps: list[RoutineStepDefinition] = Field(default_factory=list)
    notes: str = ""

    @field_validator("notes", mode="before")
    @classmethod
    def normalize_notes(cls, value: Any) -> Any:
        if isinstance(value, list):
            return "\n".join(str(item).strip() for item in value if str(item).strip())
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=True)
        return value


class ReasonerPlan(BaseModel):
    reply: str
    memory_writes: list[ReasonerMemoryPlan] = Field(default_factory=list)
    task_writes: list[ReasonerTaskPlan] = Field(default_factory=list)
    agent_writes: list[ReasonerAgentPlan] = Field(default_factory=list)
    routine_writes: list[ReasonerRoutinePlan] = Field(default_factory=list)
    tool_calls: list[ToolCallPlan] = Field(default_factory=list)
