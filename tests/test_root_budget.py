"""Tests for PRD §7 agent-supervisor root tool-call budget propagation."""
from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig


class RootBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            project_name="Root Budget Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            port=8905,
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

    def test_standalone_run_is_unchanged(self) -> None:
        agent = {"budget": {"max_tool_calls": 5, "time_budget_minutes": 30}}
        self.assertIsNone(self.app.agent_runner._derive_workflow_budget(agent, None, None))

    def test_explicit_budget_wins(self) -> None:
        agent = {"budget": {"max_tool_calls": 5}}
        explicit = {"max_tool_calls": 9}
        self.assertEqual(self.app.agent_runner._derive_workflow_budget(agent, "wf1", explicit), explicit)

    def test_unlimited_agent_unchanged_in_workflow(self) -> None:
        # No finite max_tool_calls -> nothing to ration -> unchanged.
        self.assertIsNone(
            self.app.agent_runner._derive_workflow_budget({"budget": {"time_budget_minutes": 30}}, "wf1", None)
        )

    def test_workflow_rations_shared_tool_budget(self) -> None:
        ar = self.app.agent_runner
        agent = {"budget": {"max_tool_calls": 5, "time_budget_minutes": 30}}
        # Empty workflow -> full cap; time budget preserved.
        derived = ar._derive_workflow_budget(agent, "wf1", None)
        self.assertEqual(derived["max_tool_calls"], 5)
        self.assertEqual(derived["time_budget_minutes"], 30)
        # 2 already consumed by siblings -> 3 remaining.
        ar._workflow_consumed_tool_calls = lambda wf: 2  # type: ignore[assignment]
        self.assertEqual(ar._derive_workflow_budget(agent, "wf1", None)["max_tool_calls"], 3)
        # Over budget -> clamped to 0.
        ar._workflow_consumed_tool_calls = lambda wf: 10  # type: ignore[assignment]
        self.assertEqual(ar._derive_workflow_budget(agent, "wf1", None)["max_tool_calls"], 0)

    def test_consumed_tool_calls_empty_is_zero(self) -> None:
        self.assertEqual(self.app.agent_runner._workflow_consumed_tool_calls("nonexistent"), 0)


if __name__ == "__main__":
    unittest.main()
