from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from project_q.services.workflow_service import WorkflowService
from project_q.storage import Database


def sample_definition() -> dict:
    return {
        "name": "Build and verify",
        "description": "Fan out checks and join the results.",
        "parallelism": 2,
        "nodes": [
            {
                "key": "prepare",
                "kind": "delay",
                "delay_seconds": 1,
            },
            {
                "key": "code",
                "kind": "agent",
                "agent_id": "agent_code",
                "dependencies": ["prepare"],
            },
            {
                "key": "test",
                "kind": "agent",
                "agent_id": "agent_test",
                "dependencies": ["prepare"],
            },
            {
                "key": "join",
                "kind": "tool",
                "tool_id": "diagnostics.run_self_check",
                "dependencies": ["code", "test"],
            },
        ],
    }


class WorkflowServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(self.root / "project_q.db")
        self.service = WorkflowService(self.db)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_and_update_keep_immutable_definition_versions(self) -> None:
        created = self.service.create_definition(sample_definition())
        updated_payload = sample_definition()
        updated_payload["description"] = "Second revision"
        updated_payload["parallelism"] = 3

        updated = self.service.update_definition(created["id"], updated_payload)

        self.assertEqual(created["version"], 1)
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["description"], "Second revision")
        versions = self.service.list_definition_versions(created["id"])
        self.assertEqual([item["version"] for item in versions], [2, 1])
        self.assertEqual(versions[-1]["parallelism"], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.connection() as conn:
                conn.execute(
                    "UPDATE workflow_definition_versions SET definition_json = '{}' WHERE id = ?",
                    (versions[0]["version_id"],),
                )

    def test_start_run_snapshots_definition_and_creates_pending_node_attempts(self) -> None:
        definition = self.service.create_definition(sample_definition())

        run = self.service.start_run(definition["id"], owner_approved=True)

        self.assertEqual(run["status"], "queued")
        self.assertTrue(run["owner_approved"])
        self.assertEqual(run["definition_snapshot"]["version"], 1)
        self.assertEqual(
            [node["node_key"] for node in run["nodes"]],
            ["prepare", "code", "test", "join"],
        )
        self.assertTrue(all(node["status"] == "pending" for node in run["nodes"]))
        self.assertEqual(run["events"][0]["event_type"], "workflow_run_created")
        summaries = self.service.list_runs(workflow_id=definition["id"])
        self.assertEqual(
            [node["node_key"] for node in summaries[0]["nodes"]],
            ["prepare", "code", "test", "join"],
        )

    def test_retry_pins_original_version_and_reuses_successful_nodes(self) -> None:
        definition = self.service.create_definition(sample_definition())
        first = self.service.start_run(definition["id"], owner_approved=True)
        self.service.mark_node_running(first["nodes"][0]["id"], process_id=123)
        self.service.mark_node_terminal(
            first["nodes"][0]["id"],
            status="succeeded",
            result={"artifact": "ready"},
        )
        self.service.mark_node_terminal(
            first["nodes"][1]["id"],
            status="failed",
            error="test failure",
        )
        self.service.set_run_terminal(first["id"], status="failed", error="test failure")
        changed = sample_definition()
        changed["description"] = "A later definition that must not affect retry"
        self.service.update_definition(definition["id"], changed)

        retried = self.service.start_run(
            definition["id"],
            owner_approved=True,
            retry_of_run_id=first["id"],
        )

        self.assertEqual(retried["workflow_version"], 1)
        self.assertEqual(retried["definition_snapshot"]["description"], definition["description"])
        nodes = {node["node_key"]: node for node in retried["nodes"]}
        self.assertEqual(nodes["prepare"]["status"], "succeeded")
        self.assertEqual(nodes["prepare"]["result"], {"artifact": "ready"})
        self.assertEqual(nodes["code"]["status"], "pending")
        self.assertTrue(
            any(event["event_type"] == "node_reused" for event in retried["events"])
        )

    def test_retry_does_not_reuse_prior_owner_approval_decision(self) -> None:
        definition = self.service.create_definition(
            {
                "name": "Approval retry",
                "nodes": [
                    {
                        "key": "gate",
                        "kind": "approval",
                        "prompt": "Allow this protected workflow step?",
                    },
                    {
                        "key": "work",
                        "kind": "tool",
                        "tool_id": "diagnostics.run_self_check",
                        "dependencies": ["gate"],
                    },
                ],
            }
        )
        first = self.service.start_run(definition["id"], owner_approved=False)
        waiting = self.service.mark_node_waiting_approval(first["nodes"][0]["id"])
        self.service.decide_approval(waiting["id"], approved=True, note="one-time approval")
        self.service.mark_node_running(first["nodes"][1]["id"], process_id=123)
        self.service.mark_node_terminal(first["nodes"][1]["id"], status="failed", error="boom")
        self.service.set_run_terminal(first["id"], status="failed", error="boom")

        retried = self.service.start_run(
            definition["id"],
            owner_approved=False,
            retry_of_run_id=first["id"],
        )

        nodes = {node["node_key"]: node for node in retried["nodes"]}
        self.assertEqual(nodes["gate"]["status"], "pending")
        self.assertEqual(nodes["gate"]["result"], {})
        self.assertEqual(nodes["work"]["status"], "pending")
        self.assertFalse(
            any(event["event_type"] == "node_reused" for event in retried["events"])
        )

    def test_events_are_ordered_cursor_driven_redacted_and_append_only(self) -> None:
        definition = self.service.create_definition(sample_definition())
        run = self.service.start_run(definition["id"], owner_approved=False)

        first = self.service.append_event(
            run["id"],
            event_type="node_started",
            node_run_id=run["nodes"][0]["id"],
            payload={"api_key": "secret", "safe": "visible"},
        )
        second = self.service.append_event(
            run["id"],
            event_type="node_completed",
            node_run_id=run["nodes"][0]["id"],
            payload={"result": "ok"},
        )

        items = self.service.list_events(run["id"], after_sequence=first["sequence"])

        self.assertEqual([item["sequence"] for item in items], [second["sequence"]])
        all_items = self.service.list_events(run["id"])
        started = [item for item in all_items if item["event_type"] == "node_started"][0]
        self.assertEqual(started["payload"]["api_key"], "[REDACTED]")
        self.assertEqual(started["payload"]["safe"], "visible")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.connection() as conn:
                conn.execute(
                    "UPDATE workflow_events SET event_type = 'tampered' WHERE id = ?",
                    (first["id"],),
                )

    def test_node_transitions_checkpoint_exact_state_and_cancel_is_idempotent(self) -> None:
        definition = self.service.create_definition(sample_definition())
        run = self.service.start_run(definition["id"], owner_approved=True)
        node = run["nodes"][0]

        running = self.service.mark_node_running(node["id"], process_id=321)
        completed = self.service.mark_node_terminal(
            node["id"],
            status="succeeded",
            result={"prepared": True},
        )
        checkpoints_before_cancel = self.service.list_checkpoints(run["id"])
        cancelled = self.service.request_cancel(run["id"], reason="owner stop")
        cancelled_again = self.service.request_cancel(run["id"], reason="owner stop")

        self.assertEqual(running["status"], "running")
        self.assertEqual(running["process_id"], 321)
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["result"], {"prepared": True})
        node_checkpoints = [
            item for item in checkpoints_before_cancel if item["node_run_id"] == node["id"]
        ]
        self.assertEqual(node_checkpoints[-1]["state"]["status"], "succeeded")
        self.assertEqual(cancelled["status"], "cancelling")
        self.assertEqual(cancelled_again["status"], "cancelling")

    def test_node_start_rolls_back_when_event_outbox_insert_fails(self) -> None:
        definition = self.service.create_definition(sample_definition())
        run = self.service.start_run(definition["id"], owner_approved=True)
        node = run["nodes"][0]
        self._fail_workflow_event("node_started")

        with self.assertRaises(sqlite3.IntegrityError):
            self.service.mark_node_running(node["id"], process_id=321)

        current_node = self.service.get_node_run(node["id"])
        current_run = self.service.get_run(run["id"], include_events=False)
        self.assertEqual(current_node["status"], "pending")
        self.assertIsNone(current_node["started_at"])
        self.assertIsNone(current_node["process_id"])
        self.assertEqual(current_run["status"], "queued")
        self.assertIsNone(current_run["started_at"])

    def test_start_run_rolls_back_when_creation_outbox_insert_fails(self) -> None:
        definition = self.service.create_definition(sample_definition())
        self._fail_workflow_event("workflow_run_created")

        with self.assertRaises(sqlite3.IntegrityError):
            self.service.start_run(definition["id"], owner_approved=True)

        self.assertEqual(self.service.list_runs(workflow_id=definition["id"]), [])

    def test_retry_rolls_back_when_reuse_outbox_insert_fails(self) -> None:
        definition = self.service.create_definition(sample_definition())
        first = self.service.start_run(definition["id"], owner_approved=True)
        self.service.mark_node_running(first["nodes"][0]["id"], process_id=123)
        self.service.mark_node_terminal(
            first["nodes"][0]["id"],
            status="succeeded",
            result={"artifact": "ready"},
        )
        self.service.mark_node_terminal(
            first["nodes"][1]["id"],
            status="failed",
            error="test failure",
        )
        self.service.set_run_terminal(first["id"], status="failed", error="test failure")
        self._fail_workflow_event("node_reused")

        with self.assertRaises(sqlite3.IntegrityError):
            self.service.start_run(
                definition["id"],
                owner_approved=True,
                retry_of_run_id=first["id"],
            )

        retried = [
            item
            for item in self.service.list_runs(workflow_id=definition["id"])
            if item["retry_of_run_id"] == first["id"]
        ]
        self.assertEqual(retried, [])

    def test_approval_decision_rolls_back_when_outbox_insert_fails(self) -> None:
        definition = self.service.create_definition(
            {
                "name": "Approval workflow",
                "nodes": [{"key": "approve", "kind": "approval", "prompt": "Continue?"}],
            }
        )
        run = self.service.start_run(definition["id"], owner_approved=False)
        waiting = self.service.mark_node_waiting_approval(run["nodes"][0]["id"])
        self._fail_workflow_event("approval_decided")

        with self.assertRaises(sqlite3.IntegrityError):
            self.service.decide_approval(waiting["id"], approved=True, note="yes")

        current = self.service.get_node_run(waiting["id"])
        self.assertEqual(current["status"], "waiting_approval")
        self.assertEqual(current["result"], {})
        self.assertIsNone(current["completed_at"])

    def test_node_terminal_rolls_back_when_outbox_insert_fails(self) -> None:
        definition = self.service.create_definition(sample_definition())
        run = self.service.start_run(definition["id"], owner_approved=True)
        running = self.service.mark_node_running(run["nodes"][0]["id"], process_id=321)
        self._fail_workflow_event("node_succeeded")

        with self.assertRaises(sqlite3.IntegrityError):
            self.service.mark_node_terminal(
                running["id"],
                status="succeeded",
                result={"prepared": True},
            )

        current = self.service.get_node_run(running["id"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["result"], {})
        self.assertIsNone(current["completed_at"])

    def test_run_terminal_rolls_back_when_outbox_insert_fails(self) -> None:
        definition = self.service.create_definition(sample_definition())
        run = self.service.start_run(definition["id"], owner_approved=True)
        self._fail_workflow_event("workflow_completed")

        with self.assertRaises(sqlite3.IntegrityError):
            self.service.set_run_terminal(
                run["id"],
                status="completed",
                result={"ok": True},
            )

        current = self.service.get_run(run["id"], include_events=False)
        self.assertEqual(current["status"], "queued")
        self.assertEqual(current["result"], {})
        self.assertIsNone(current["completed_at"])

    def test_cancel_rolls_back_when_outbox_insert_fails(self) -> None:
        definition = self.service.create_definition(sample_definition())
        run = self.service.start_run(definition["id"], owner_approved=True)
        self._fail_workflow_event("workflow_cancellation_requested")

        with self.assertRaises(sqlite3.IntegrityError):
            self.service.request_cancel(run["id"], reason="owner stop")

        current = self.service.get_run(run["id"], include_events=False)
        self.assertEqual(current["status"], "queued")
        self.assertFalse(current["cancel_requested"])
        self.assertEqual(current["cancel_reason"], "")

    def test_late_success_cannot_overwrite_a_cancelling_run(self) -> None:
        definition = self.service.create_definition(sample_definition())
        run = self.service.start_run(definition["id"], owner_approved=True)
        self.service.request_cancel(run["id"], reason="owner cancelled")

        with self.assertRaises(ValueError):
            self.service.set_run_terminal(run["id"], status="completed")

        self.assertEqual(self.service.get_run(run["id"])["status"], "cancelling")

    def test_definition_validation_rejects_unsafe_or_incomplete_nodes(self) -> None:
        payload = sample_definition()
        payload["nodes"][0]["delay_seconds"] = 0
        with self.assertRaises(ValueError):
            self.service.create_definition(payload)

        payload = sample_definition()
        payload["nodes"][1].pop("agent_id")
        with self.assertRaises(ValueError):
            self.service.create_definition(payload)

        payload = sample_definition()
        payload["nodes"][3]["payload"] = {"nested": {"api_key": "must be allowed but redacted in events"}}
        created = self.service.create_definition(payload)
        self.assertEqual(created["nodes"][-1]["payload"]["nested"]["api_key"], "must be allowed but redacted in events")

    def test_owner_approval_node_waits_and_persists_decision(self) -> None:
        definition = self.service.create_definition(
            {
                "name": "Approval workflow",
                "nodes": [
                    {
                        "key": "approve",
                        "kind": "approval",
                        "prompt": "Publish the generated report?",
                    }
                ],
            }
        )
        run = self.service.start_run(definition["id"], owner_approved=False)
        waiting = self.service.mark_node_waiting_approval(run["nodes"][0]["id"])

        decided = self.service.decide_approval(
            waiting["id"],
            approved=True,
            note="Reviewed by owner",
        )

        self.assertEqual(waiting["status"], "waiting_approval")
        self.assertEqual(decided["status"], "succeeded")
        self.assertEqual(decided["result"]["approved"], True)
        self.assertEqual(decided["result"]["note"], "Reviewed by owner")
        events = self.service.list_events(run["id"])
        self.assertTrue(any(item["event_type"] == "approval_requested" for item in events))
        self.assertTrue(any(item["event_type"] == "approval_decided" for item in events))

    def _fail_workflow_event(self, event_type: str) -> None:
        trigger_name = f"fail_{event_type}_event_insert"
        with self.db.connection() as conn:
            conn.execute(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE INSERT ON workflow_events
                WHEN NEW.event_type = '{event_type}'
                BEGIN
                    SELECT RAISE(ABORT, 'forced workflow event failure');
                END
                """
            )


if __name__ == "__main__":
    unittest.main()
