from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig


class WorkflowSubprocessEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.app = create_application(
            AppConfig(
                project_name="Project Q Test",
                workspace_root=root,
                data_root=root / ".project_q",
                db_path=root / ".project_q" / "project_q.db",
            )
        )

    def tearDown(self) -> None:
        self.app.shutdown_services()
        self.temp_dir.cleanup()

    def wait_for_status(self, run_id: str, statuses: set[str], timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.app.workflows.get_run(run_id)
            if run["status"] in statuses:
                return run
            time.sleep(0.1)
        self.fail(f"workflow {run_id} did not reach {sorted(statuses)}")

    def test_real_subprocess_completes_and_persists_result(self) -> None:
        definition = self.app.workflows.create_definition(
            {
                "name": "Real worker",
                "nodes": [{"key": "pause", "kind": "delay", "delay_seconds": 1}],
            }
        )

        run = self.app.workflow_orchestrator.submit(
            definition["id"],
            owner_approved=True,
        )
        completed = self.wait_for_status(run["id"], {"completed", "failed"})

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["nodes"][0]["status"], "succeeded")
        self.assertEqual(completed["nodes"][0]["result"], {"delay_seconds": 1})

    def test_real_subprocess_uses_explicit_custom_data_root(self) -> None:
        self.app.shutdown_services()
        root = Path(self.temp_dir.name)
        custom_data = root / "custom-runtime-data"
        self.app = create_application(
            AppConfig(
                project_name="Project Q Custom Data Test",
                workspace_root=root,
                data_root=custom_data,
                db_path=custom_data / "custom-project-q.db",
            )
        )
        definition = self.app.workflows.create_definition(
            {
                "name": "Custom data worker",
                "nodes": [{"key": "pause", "kind": "delay", "delay_seconds": 1}],
            }
        )

        run = self.app.workflow_orchestrator.submit(
            definition["id"],
            owner_approved=True,
        )
        completed = self.wait_for_status(run["id"], {"completed", "failed"})

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["nodes"][0]["result"], {"delay_seconds": 1})

    def test_real_subprocess_is_terminated_when_workflow_is_cancelled(self) -> None:
        definition = self.app.workflows.create_definition(
            {
                "name": "Cancelable worker",
                "nodes": [{"key": "pause", "kind": "delay", "delay_seconds": 30}],
            }
        )
        run = self.app.workflow_orchestrator.submit(
            definition["id"],
            owner_approved=True,
        )
        running = self.wait_for_status(run["id"], {"running", "failed"})
        self.assertEqual(running["status"], "running")
        deadline = time.monotonic() + 5
        process_id = running["nodes"][0]["process_id"]
        while process_id is None and time.monotonic() < deadline:
            time.sleep(0.05)
            running = self.app.workflows.get_run(run["id"])
            process_id = running["nodes"][0]["process_id"]
        self.assertIsNotNone(process_id)

        self.app.workflow_orchestrator.cancel(run["id"], reason="test cancellation")
        cancelled = self.wait_for_status(run["id"], {"cancelled", "failed"})

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["nodes"][0]["status"], "cancelled")
        deadline = time.monotonic() + 3
        while (
            self.app.workflow_orchestrator.node_backend.pid_is_running(process_id)
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        self.assertFalse(self.app.workflow_orchestrator.node_backend.pid_is_running(process_id))

    def test_real_subprocess_is_terminated_when_node_times_out(self) -> None:
        definition = self.app.workflows.create_definition(
            {
                "name": "Timeout worker",
                "nodes": [
                    {
                        "key": "pause",
                        "kind": "delay",
                        "delay_seconds": 30,
                        "timeout_seconds": 1,
                    }
                ],
            }
        )
        run = self.app.workflow_orchestrator.submit(
            definition["id"],
            owner_approved=True,
        )

        failed = self.wait_for_status(run["id"], {"failed"}, timeout=10)

        self.assertEqual(failed["nodes"][0]["status"], "failed")
        self.assertIn("timed out", failed["nodes"][0]["error"])


if __name__ == "__main__":
    unittest.main()
