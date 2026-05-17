from __future__ import annotations

from typing import Any

from project_q.tools.base import ToolDefinition


class TrainingExportDatasetTool:
    definition = ToolDefinition(
        tool_id="training.export_dataset",
        name="Export Training Dataset",
        description="Export Project Q conversations, lessons, and routing evals for local LoRA weight training",
        tier=1,
    )

    def __init__(self, training_service) -> None:
        self.training_service = training_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        reason = str(payload.get("reason", "tool")).strip() or "tool"
        max_records = int(payload.get("max_records", 200) or 200)
        return self.training_service.export_dataset(reason=reason, max_records=max_records)


class TrainingCapabilityPlanTool:
    definition = ToolDefinition(
        tool_id="training.capability_plan",
        name="Project Q Capability Plan",
        description="Explain the concrete path toward ChatGPT/Codex/Claude-like local agent capability",
        tier=0,
    )

    def __init__(self, training_service) -> None:
        self.training_service = training_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        return self.training_service.capability_plan()


class TrainingPrepareLoraJobTool:
    definition = ToolDefinition(
        tool_id="training.prepare_lora_job",
        name="Prepare LoRA Training Job",
        description="Create a local dry-run-first LoRA weight-training job from Project Q datasets",
        tier=2,
    )

    def __init__(self, training_service) -> None:
        self.training_service = training_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        reason = str(payload.get("reason", "tool")).strip() or "tool"
        base_model = str(payload.get("base_model", "Qwen/Qwen2.5-Coder-1.5B-Instruct")).strip()
        max_records = int(payload.get("max_records", 300) or 300)
        max_steps = int(payload.get("max_steps", 120) or 120)
        max_seq_length = int(payload.get("max_seq_length", 1536) or 1536)
        return self.training_service.prepare_lora_job(
            reason=reason,
            base_model=base_model,
            max_records=max_records,
            max_steps=max_steps,
            max_seq_length=max_seq_length,
        )
