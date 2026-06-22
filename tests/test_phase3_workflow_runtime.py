from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.models import AgentCreate
from project_q.workflow_worker import execute_node


class WorkflowRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def config(self, *, worker_mode: bool = False) -> AppConfig:
        return AppConfig(
            project_name="Project Q Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            worker_mode=worker_mode,
        )

    def test_application_starts_orchestrator_and_shutdown_stops_it(self) -> None:
        app = create_application(self.config())
        try:
            self.assertIsNotNone(app.workflows)
            self.assertIsNotNone(app.workflow_orchestrator)
            self.assertIsNotNone(app.workflow_orchestrator._thread)
            self.assertTrue(app.workflow_orchestrator._thread.is_alive())
        finally:
            app.shutdown_services()
        self.assertIsNone(app.workflow_orchestrator._thread)

    def test_worker_mode_disables_scheduler_and_nested_orchestrator(self) -> None:
        app = create_application(self.config(worker_mode=True))
        try:
            self.assertIsNone(app.scheduler)
            self.assertIsNone(app.workflow_orchestrator._thread)
        finally:
            app.shutdown_services()

    def test_discover_reads_worker_mode_from_environment(self) -> None:
        with patch.dict(os.environ, {"PROJECT_Q_WORKER_MODE": "1"}):
            config = AppConfig.discover(self.root)
        self.assertTrue(config.worker_mode)

    def test_kill_switch_cancels_all_workflows(self) -> None:
        app = create_application(self.config(worker_mode=True))
        cancel_all = MagicMock(return_value=["workflowrun_1"])
        app.workflow_orchestrator.cancel_all = cancel_all
        try:
            app.control.activate(reason="stop everything", source="test")
        finally:
            app.shutdown_services()
        cancel_all.assert_called_once_with(reason="kill switch: stop everything")

    def test_worker_executes_authorized_tool_node_and_persists_result(self) -> None:
        app = create_application(self.config(worker_mode=True))
        tool = MagicMock()
        tool.definition.tier = 0
        tool.execute.return_value = {"ok": True}
        app.tools.get = MagicMock(return_value=tool)
        definition = app.workflows.create_definition(
            {
                "name": "Worker test",
                "nodes": [
                    {
                        "key": "work",
                        "kind": "tool",
                        "tool_id": "test.tool",
                    }
                ],
            }
        )
        run = app.workflows.start_run(definition["id"], owner_approved=True)
        node = app.workflows.mark_node_running(run["nodes"][0]["id"], process_id=None)
        try:
            return_code = execute_node(app, node["id"])
            current = app.workflows.get_node_run(node["id"])
        finally:
            app.shutdown_services()

        self.assertEqual(return_code, 0)
        self.assertEqual(current["status"], "succeeded")
        self.assertEqual(current["result"], {"ok": True})
        tool.execute.assert_called_once_with({})

    def test_worker_links_agent_run_to_workflow_and_node(self) -> None:
        app = create_application(self.config(worker_mode=True))
        agent = app.agents.create(
            AgentCreate(name="Workflow agent", goal="Record workflow provenance", status="active")
        )
        app.agent_runner.run = MagicMock(return_value={"id": "agentrun_1", "outcome": "completed"})
        definition = app.workflows.create_definition(
            {
                "name": "Agent worker test",
                "nodes": [
                    {
                        "key": "agent",
                        "kind": "agent",
                        "agent_id": agent["id"],
                    }
                ],
            }
        )
        run = app.workflows.start_run(definition["id"], owner_approved=True)
        node = app.workflows.mark_node_running(run["nodes"][0]["id"], process_id=None)
        try:
            return_code = execute_node(app, node["id"])
        finally:
            app.shutdown_services()

        self.assertEqual(return_code, 0)
        app.agent_runner.run.assert_called_once_with(
            agent["id"],
            owner_approved=True,
            workflow_run_id=run["id"],
            workflow_node_run_id=node["id"],
        )


if __name__ == "__main__":
    unittest.main()
