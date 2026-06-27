"""Tests for the observe-then-replan synthesis pass.

The pass is gated (observe_then_replan_enabled, default off), only active with a
configured provider, and synthesis-only — it re-plans for a better grounded
reply but NEVER re-executes tools.
"""
from __future__ import annotations

import json
import shutil
import unittest
import uuid
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.models import ChatRequest, SettingsUpdate
from project_q.services.conversation import ConversationService
from project_q.services.reasoner import ReasonerService


def _ollama_reply(text: str):
    """Fake transport returning an Ollama-shaped plan whose reply is TEXT, and
    recording the request body so tests can assert observations were fed back."""
    seen: dict[str, str] = {}

    def transport(_url, body, _headers, _timeout_seconds):
        seen["body"] = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
        return {
            "message": {
                "content": json.dumps(
                    {
                        "reply": text,
                        "memory_writes": [],
                        "task_writes": [],
                        "agent_writes": [],
                        "routine_writes": [],
                        "tool_calls": [],
                    }
                )
            }
        }

    return transport, seen


class _AppFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            project_name="Observe Replan Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            port=8904,
        )
        self.app = create_application(config)

    def tearDown(self) -> None:
        sd = getattr(self.app, "shutdown_services", None)
        if callable(sd):
            try:
                sd()
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _enable_ollama_provider(self) -> None:
        self.app.settings.update(SettingsUpdate(provider_enabled=True, provider_type="ollama"))


class SynthesizeUnitTests(_AppFixture):
    OBS = [{"tool_id": "research.web", "result": {"summary": "Across 2 sources: X"}}]

    def _reasoner(self, transport):
        return ReasonerService(self.app.settings, self.app.vault, self.app.audit, transport=transport)

    def test_returns_none_when_feature_disabled(self) -> None:
        self._enable_ollama_provider()  # provider on, observe flag off (default)
        transport, _ = _ollama_reply("REFINED")
        reasoner = self._reasoner(transport)
        self.assertIsNone(reasoner.synthesize(user_message="q", observations=self.OBS))

    def test_returns_none_without_provider(self) -> None:
        self.app.settings.update(SettingsUpdate(observe_then_replan_enabled=True))
        transport, _ = _ollama_reply("REFINED")
        reasoner = self._reasoner(transport)
        self.assertIsNone(reasoner.synthesize(user_message="q", observations=self.OBS))

    def test_returns_none_without_observations(self) -> None:
        self._enable_ollama_provider()
        self.app.settings.update(SettingsUpdate(observe_then_replan_enabled=True))
        transport, _ = _ollama_reply("REFINED")
        reasoner = self._reasoner(transport)
        self.assertIsNone(reasoner.synthesize(user_message="q", observations=[]))

    def test_refines_and_feeds_observations_back(self) -> None:
        self._enable_ollama_provider()
        self.app.settings.update(SettingsUpdate(observe_then_replan_enabled=True))
        transport, seen = _ollama_reply("Grounded answer from observations.")
        reasoner = self._reasoner(transport)
        out = reasoner.synthesize(user_message="summarize", observations=self.OBS)
        self.assertEqual(out, "Grounded answer from observations.")
        # The observations were serialized into the provider request.
        self.assertIn("tool_observations", seen["body"])
        self.assertIn("Across 2 sources", seen["body"])


class ObservationFilterTests(unittest.TestCase):
    def _service(self) -> ConversationService:
        return ConversationService(None, None, None, None, None, None, None)

    def test_keeps_read_only_results_and_drops_writes_and_empties(self) -> None:
        svc = self._service()
        executed = [
            {"tool_id": "research.web", "result": {"summary": "hi"}},
            {"tool_id": "filesystem.write_file", "result": {"path": "x"}},  # write -> dropped
            {"tool_id": "knowledge.answer", "result": ""},  # empty -> dropped
            {"tool_id": "filesystem.read_file", "result": "file body"},
        ]
        obs = svc._observations_from(executed)
        self.assertEqual([o["tool_id"] for o in obs], ["research.web", "filesystem.read_file"])


class _FakeExecutor:
    def __init__(self, executed_tools):
        self._executed = executed_tools
        self.execute_calls = 0

    def execute(self, **_kwargs):
        self.execute_calls += 1
        return {
            "executed_tools": self._executed,
            "blocked_tools": [],
            "created_task_ids": [],
            "created_memory_ids": [],
            "created_agent_ids": [],
            "created_routine_ids": [],
            "created_action_request_ids": [],
        }

    def compose_reply(self, reply, _executed, _blocked, _warning):
        return reply  # echo so the test can see which reply text was used


class ConversationRefineIntegrationTests(_AppFixture):
    def _wire(self, refined):
        conv = self.app.conversations
        conv.executor_service = _FakeExecutor([{"tool_id": "research.web", "result": {"summary": "obs"}}])
        conv.reasoner_service.synthesize = lambda **_kw: refined
        return conv

    def test_refined_reply_is_used_and_tools_not_reexecuted(self) -> None:
        conv = self._wire("REFINED-GROUNDED-ANSWER")
        resp = conv.respond(ChatRequest(message="please research X"))
        self.assertEqual(resp.reply, "REFINED-GROUNDED-ANSWER")
        self.assertEqual(conv.executor_service.execute_calls, 1)  # no second execution

    def test_single_pass_reply_kept_when_refine_returns_none(self) -> None:
        conv = self._wire(None)
        resp = conv.respond(ChatRequest(message="please research X"))
        self.assertEqual(conv.executor_service.execute_calls, 1)
        self.assertIsInstance(resp.reply, str)


if __name__ == "__main__":
    unittest.main()
