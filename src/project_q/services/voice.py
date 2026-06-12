from __future__ import annotations

from typing import Any

from project_q.models import ChatRequest


class VoiceService:
    def __init__(self, settings_service, conversation_service, audit_service) -> None:
        self.settings_service = settings_service
        self.conversation_service = conversation_service
        self.audit_service = audit_service

    def ingest_transcript(
        self,
        *,
        transcript: str,
        owner_approved: bool = False,
        source: str = "desktop_microphone",
    ) -> dict[str, Any]:
        settings = self.settings_service.get_all()
        if not settings.get("voice_enabled", False):
            raise PermissionError("voice input is disabled in settings")
        text = str(transcript or "").strip()
        if not text:
            raise ValueError("voice transcript is required")
        source_text = str(source or "desktop_microphone").strip()[:100] or "desktop_microphone"

        response = self.conversation_service.respond(
            ChatRequest(message=text, owner_approved=owner_approved),
        )
        self.audit_service.log(
            action_type="voice_transcript",
            action_tier=0,
            tool_name="voice_service",
            outcome="completed",
            approved_by_owner=owner_approved,
            input_sources=["owner", "voice_transcript", source_text],
            metadata={
                "source": source_text,
                "transcript_length": len(text),
                "created_task_ids": response.created_task_ids,
                "created_memory_ids": response.created_memory_ids,
                "created_agent_ids": response.created_agent_ids,
                "created_routine_ids": response.created_routine_ids,
                "executed_tool_ids": [item["tool_id"] for item in response.executed_tools],
                "blocked_tool_ids": [item["tool_id"] for item in response.blocked_tools],
            },
        )
        return {
            "source": source_text,
            "transcript": text,
            "response": response,
        }
