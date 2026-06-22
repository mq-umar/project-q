from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from project_q.models import ChatRequest, ChatResponse, utc_now
from project_q.services.reasoner import ReasonerResult


@dataclass(slots=True)
class ConversationStreamEvent:
    token: str = ""
    response: ChatResponse | None = None


class ConversationService:
    def __init__(
        self,
        db,
        memory_service,
        task_service,
        agent_service,
        context_service,
        reasoner_service,
        executor_service,
        sync_service=None,
    ) -> None:
        self.db = db
        self.context_service = context_service
        self.reasoner_service = reasoner_service
        self.executor_service = executor_service
        self.sync_service = sync_service

    def respond(
        self,
        payload: ChatRequest,
        *,
        channel: str = "text",
        input_sources: list[str] | None = None,
    ) -> ChatResponse:
        message = payload.message.strip()
        self._save_message("user", message, channel=channel)

        context = self.context_service.build(message)
        reasoning = self.reasoner_service.plan(user_message=message, context=context)
        return self._finalize_response(
            payload=payload,
            reasoning=reasoning,
            channel=channel,
            input_sources=input_sources or ["owner", "reasoner"],
        )

    def respond_stream(
        self,
        payload: ChatRequest,
        *,
        channel: str = "text",
        input_sources: list[str] | None = None,
    ) -> Iterator[ConversationStreamEvent]:
        message = payload.message.strip()
        self._save_message("user", message, channel=channel)

        context = self.context_service.build(message)
        emitted_reply = ""
        reasoning: ReasonerResult | None = None
        for event in self.reasoner_service.stream_plan(user_message=message, context=context):
            if event.token:
                emitted_reply += event.token
                yield ConversationStreamEvent(token=event.token)
            if event.result is not None:
                reasoning = event.result

        if reasoning is None:
            raise RuntimeError("streaming reasoner completed without a final plan")

        response = self._finalize_response(
            payload=payload,
            reasoning=reasoning,
            channel=channel,
            input_sources=input_sources or ["owner", "reasoner"],
        )
        if not emitted_reply:
            yield ConversationStreamEvent(token=response.reply)
        elif response.reply.startswith(emitted_reply):
            suffix = response.reply[len(emitted_reply) :]
            if suffix:
                yield ConversationStreamEvent(token=suffix)
        yield ConversationStreamEvent(response=response)

    def _finalize_response(
        self,
        *,
        payload: ChatRequest,
        reasoning: ReasonerResult,
        channel: str,
        input_sources: list[str],
    ) -> ChatResponse:
        execution = self.executor_service.execute(
            plan=reasoning.plan,
            owner_approved=payload.owner_approved,
            input_sources=input_sources,
            originating_goal=payload.message,
            model=reasoning.model_name or "",
        )
        reply = self.executor_service.compose_reply(
            reasoning.plan.reply,
            execution["executed_tools"],
            execution["blocked_tools"],
            reasoning.warning,
        )
        self._save_message("assistant", reply, channel=channel)

        return ChatResponse(
            reply=reply,
            created_task_ids=execution["created_task_ids"],
            created_memory_ids=execution["created_memory_ids"],
            created_agent_ids=execution["created_agent_ids"],
            created_routine_ids=execution["created_routine_ids"],
            executed_tools=execution["executed_tools"],
            blocked_tools=execution["blocked_tools"],
            created_action_request_ids=execution["created_action_request_ids"],
            reasoning_mode=reasoning.mode,
            model_name=reasoning.model_name,
            context_summary=self.context_service.summary_counts(),
        )

    def list_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM conversations
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "channel": row["channel"],
                "metadata": self.db.loads(row["metadata_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _save_message(self, role: str, content: str, *, channel: str = "text") -> None:
        message_id = self.db.make_id("msg")
        created_at = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO conversations (id, role, content, channel, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (message_id, role, content, channel, "{}", created_at),
            )
        if self.sync_service is not None:
            self.sync_service.append(
                resource_type="conversation",
                resource_id=message_id,
                operation="created",
                payload={
                    "id": message_id,
                    "role": role,
                    "content": content,
                    "channel": channel,
                    "metadata": {},
                    "created_at": created_at,
                },
            )
