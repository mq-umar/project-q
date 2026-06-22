from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from project_q.services.workflow_orchestrator import WorkflowOrchestratorService
from project_q.services.workflow_service import WorkflowService
from project_q.storage import Database


class FakeHandle:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.return_code = None
        self.cancelled = False
        self.closed = False

    def poll(self):
        return self.return_code

    def cancel(self):
        self.cancelled = True
        self.return_code = 143
        return self.return_code

    def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, FakeHandle]] = []
        self.running_pids: set[int] = set()
        self.cancelled_pids: list[int] = []

    def start(self, run_id: str, node_run_id: str) -> FakeHandle:
        handle = FakeHandle(1000 + len(self.started))
        self.started.append((run_id, node_run_id, handle))
        self.running_pids.add(handle.pid)
        return handle

    def pid_is_running(self, process_id: int) -> bool:
        return process_id in self.running_pids

    def cancel_pid(self, process_id: int) -> None:
        self.cancelled_pids.append(process_id)
        self.running_pids.discard(process_id)


def workflow(nodes, parallelism=2):
    return {
        "name": "Orchestration test",
        "parallelism": parallelism,
        "nodes": nodes,
    }


class WorkflowOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "project_q.db")
        self.workflows = WorkflowService(self.db)
        self.backend = FakeBackend()
        self.orchestrator = WorkflowOrchestratorService(
            self.workflows,
            self.backend,
            poll_interval_seconds=0.01,
        )

    def tearDown(self) -> None:
        self.orchestrator.stop()
        self.temp_dir.cleanup()

    def complete(self, node_run_id: str, result=None) -> None:
        self.workflows.mark_node_terminal(
            node_run_id,
            status="succeeded",
            result=result or {"ok": True},
        )
        for _, candidate, handle in self.backend.started:
            if candidate == node_run_id:
                handle.return_code = 0
                self.backend.running_pids.discard(handle.pid)

    def test_fan_out_and_fan_in_run_in_dependency_order(self) -> None:
        definition = self.workflows.create_definition(
            workflow(
                [
                    {"key": "root", "kind": "delay", "delay_seconds": 1},
                    {
                        "key": "alpha",
                        "kind": "tool",
                        "tool_id": "test.alpha",
                        "dependencies": ["root"],
                    },
                    {
                        "key": "beta",
                        "kind": "tool",
                        "tool_id": "test.beta",
                        "dependencies": ["root"],
                    },
                    {
                        "key": "join",
                        "kind": "tool",
                        "tool_id": "test.join",
                        "dependencies": ["alpha", "beta"],
                    },
                ]
            )
        )
        run = self.orchestrator.submit(definition["id"], owner_approved=True)

        self.orchestrator.run_once()
        self.assertEqual([self.workflows.get_node_run(item[1])["node_key"] for item in self.backend.started], ["root"])
        self.complete(self.backend.started[0][1])

        self.orchestrator.run_once()
        self.assertEqual(
            [self.workflows.get_node_run(item[1])["node_key"] for item in self.backend.started],
            ["root", "alpha", "beta"],
        )
        self.complete(self.backend.started[1][1])
        self.orchestrator.run_once()
        self.assertEqual(len(self.backend.started), 3)
        self.complete(self.backend.started[2][1])

        self.orchestrator.run_once()
        self.assertEqual(self.workflows.get_node_run(self.backend.started[3][1])["node_key"], "join")
        self.complete(self.backend.started[3][1])
        self.orchestrator.run_once()

        completed = self.workflows.get_run(run["id"])
        self.assertEqual(completed["status"], "completed")

    def test_same_agent_nodes_are_serialized_even_with_parallel_capacity(self) -> None:
        definition = self.workflows.create_definition(
            workflow(
                [
                    {"key": "one", "kind": "agent", "agent_id": "shared"},
                    {"key": "two", "kind": "agent", "agent_id": "shared"},
                ],
                parallelism=2,
            )
        )
        self.orchestrator.submit(definition["id"], owner_approved=True)

        self.orchestrator.run_once()
        self.assertEqual(len(self.backend.started), 1)
        self.complete(self.backend.started[0][1])
        self.orchestrator.run_once()
        self.assertEqual(len(self.backend.started), 2)

    def test_cancel_stops_live_workers_and_marks_pending_nodes_cancelled(self) -> None:
        definition = self.workflows.create_definition(
            workflow(
                [
                    {"key": "running", "kind": "delay", "delay_seconds": 10},
                    {
                        "key": "later",
                        "kind": "tool",
                        "tool_id": "test.later",
                        "dependencies": ["running"],
                    },
                ]
            )
        )
        run = self.orchestrator.submit(definition["id"], owner_approved=True)
        self.orchestrator.run_once()
        handle = self.backend.started[0][2]

        self.orchestrator.cancel(run["id"], reason="owner pressed stop")
        self.orchestrator.run_once()

        cancelled = self.workflows.get_run(run["id"])
        self.assertTrue(handle.cancelled)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual({item["status"] for item in cancelled["nodes"]}, {"cancelled"})

    def test_unreported_worker_exit_fails_the_node_and_workflow(self) -> None:
        definition = self.workflows.create_definition(
            workflow([{"key": "work", "kind": "tool", "tool_id": "test.work"}])
        )
        run = self.orchestrator.submit(definition["id"], owner_approved=True)
        self.orchestrator.run_once()
        handle = self.backend.started[0][2]
        handle.return_code = 2
        self.backend.running_pids.discard(handle.pid)

        self.orchestrator.run_once()

        failed = self.workflows.get_run(run["id"])
        self.assertEqual(failed["nodes"][0]["status"], "failed")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("exited", failed["nodes"][0]["error"])

    def test_dispatch_cleans_up_worker_when_pid_assignment_fails(self) -> None:
        definition = self.workflows.create_definition(
            workflow([{"key": "work", "kind": "tool", "tool_id": "test.work"}])
        )
        run = self.orchestrator.submit(definition["id"], owner_approved=True)

        def fail_set_process_id(_node_run_id, _process_id):
            raise ValueError("database write failed")

        self.workflows.set_node_process_id = fail_set_process_id

        self.orchestrator.run_once()

        handle = self.backend.started[0][2]
        failed = self.workflows.get_run(run["id"])
        self.assertTrue(handle.cancelled)
        self.assertTrue(handle.closed)
        self.assertEqual(failed["nodes"][0]["status"], "failed")
        self.assertIn("database write failed", failed["nodes"][0]["error"])
        self.assertEqual(self.orchestrator._handles, {})

    def test_timeout_terminates_worker_and_fails_node(self) -> None:
        definition = self.workflows.create_definition(
            workflow(
                [
                    {
                        "key": "slow",
                        "kind": "delay",
                        "delay_seconds": 30,
                        "timeout_seconds": 1,
                    }
                ]
            )
        )
        run = self.orchestrator.submit(definition["id"], owner_approved=True)
        self.orchestrator.run_once()
        handle = self.backend.started[0][2]
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE workflow_node_runs
                SET started_at = '2000-01-01T00:00:00Z'
                WHERE id = ?
                """,
                (self.backend.started[0][1],),
            )

        self.orchestrator.run_once()

        failed = self.workflows.get_run(run["id"])
        self.assertTrue(handle.cancelled)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["nodes"][0]["status"], "failed")
        self.assertIn("timed out", failed["nodes"][0]["error"])

    def test_human_checkpoint_pauses_without_worker_then_continues_after_approval(self) -> None:
        definition = self.workflows.create_definition(
            workflow(
                [
                    {
                        "key": "gate",
                        "kind": "approval",
                        "prompt": "Continue to the protected action?",
                    },
                    {
                        "key": "after",
                        "kind": "tool",
                        "tool_id": "test.after",
                        "dependencies": ["gate"],
                    },
                ]
            )
        )
        run = self.orchestrator.submit(definition["id"], owner_approved=False)

        self.orchestrator.run_once()
        waiting = self.workflows.get_run(run["id"])
        self.assertEqual(waiting["nodes"][0]["status"], "waiting_approval")
        self.assertEqual(self.backend.started, [])

        self.workflows.decide_approval(
            waiting["nodes"][0]["id"],
            approved=True,
            note="owner approved",
        )
        self.orchestrator.run_once()

        self.assertEqual(
            self.workflows.get_node_run(self.backend.started[0][1])["node_key"],
            "after",
        )

    def test_restart_reconciliation_fails_missing_process_without_duplicate_dispatch(self) -> None:
        definition = self.workflows.create_definition(
            workflow([{"key": "work", "kind": "tool", "tool_id": "test.work"}])
        )
        run = self.workflows.start_run(definition["id"], owner_approved=True)
        node = run["nodes"][0]
        self.workflows.mark_node_running(node["id"], process_id=777)

        self.orchestrator.reconcile()
        self.orchestrator.run_once()

        failed = self.workflows.get_run(run["id"])
        self.assertEqual(self.backend.started, [])
        self.assertEqual(failed["nodes"][0]["status"], "failed")
        self.assertEqual(failed["status"], "failed")

    def test_stop_terminates_owned_workers_and_persists_cancellation(self) -> None:
        definition = self.workflows.create_definition(
            workflow([{"key": "work", "kind": "delay", "delay_seconds": 30}])
        )
        run = self.orchestrator.submit(definition["id"], owner_approved=True)
        self.orchestrator.run_once()
        handle = self.backend.started[0][2]

        self.orchestrator.stop()

        current = self.workflows.get_run(run["id"])
        self.assertTrue(handle.cancelled)
        self.assertTrue(handle.closed)
        self.assertEqual(current["status"], "cancelled")
        self.assertEqual(current["nodes"][0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
