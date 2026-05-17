from __future__ import annotations

from typing import Any

from project_q.models import ChatRequest, ChatResponse, utc_now


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
    ) -> None:
        self.db = db
        self.context_service = context_service
        self.reasoner_service = reasoner_service
        self.executor_service = executor_service

    def respond(self, payload: ChatRequest) -> ChatResponse:
        message = payload.message.strip()
        self._save_message("user", message)

        context = self.context_service.build(message)
        reasoning = self.reasoner_service.plan(user_message=message, context=context)
        execution = self.executor_service.execute(
            plan=reasoning.plan,
            owner_approved=payload.owner_approved,
            input_sources=["owner", "reasoner"],
        )
        reply = self.executor_service.compose_reply(
            reasoning.plan.reply,
            execution["executed_tools"],
            execution["blocked_tools"],
            reasoning.warning,
        )
        self._save_message("assistant", reply)

        return ChatResponse(
            reply=reply,
            created_task_ids=execution["created_task_ids"],
            created_memory_ids=execution["created_memory_ids"],
            created_agent_ids=execution["created_agent_ids"],
            created_routine_ids=execution["created_routine_ids"],
            executed_tools=execution["executed_tools"],
            blocked_tools=execution["blocked_tools"],
            reasoning_mode=reasoning.mode,
            model_name=reasoning.model_name,
            context_summary=self.context_service.summary_counts(),
        )

    def list_messages(self, limit: int = 100) -> list[dict[str, Any]]:
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

    def _save_message(self, role: str, content: str) -> None:
        message_id = self.db.make_id("msg")
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO conversations (id, role, content, channel, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (message_id, role, content, "text", "{}", utc_now()),
            )
