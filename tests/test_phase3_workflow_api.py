from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.server import ProjectQHandler


class WorkflowApiTests(unittest.TestCase):
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
        handler = type("WorkflowApiHandler", (ProjectQHandler,), {"app": self.app})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        response = urllib.request.urlopen(f"{self.base_url}/", timeout=10)
        self.cookie = response.headers["Set-Cookie"].split(";", 1)[0]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.app.shutdown_services()
        self.temp_dir.cleanup()

    def request(self, method: str, path: str, payload=None, *, owner=True):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if owner:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        response = urllib.request.urlopen(request, timeout=10)
        return response.status, json.loads(response.read().decode("utf-8"))

    def error_response(self, error: urllib.error.HTTPError) -> dict[str, object]:
        return json.loads(error.read().decode("utf-8"))

    def create_workflow(self):
        return self.request(
            "POST",
            "/api/workflows",
            {
                "name": "API workflow",
                "parallelism": 2,
                "nodes": [
                    {"key": "wait", "kind": "delay", "delay_seconds": 30},
                ],
            },
        )

    def wait_for_run_status(self, run_id: str, statuses: set[str], timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, run = self.request("GET", f"/api/workflow-runs/{run_id}")
            if run["status"] in statuses:
                return run
            time.sleep(0.1)
        self.fail(f"workflow {run_id} did not reach {sorted(statuses)}")

    def test_workflow_endpoints_require_owner_session(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request("GET", "/api/workflows", owner=False)
        self.assertEqual(captured.exception.code, 401)

    def test_crud_start_detail_events_results_cancel_and_retry(self) -> None:
        status, created = self.create_workflow()
        self.assertEqual(status, 201)

        status, listing = self.request("GET", "/api/workflows")
        self.assertEqual(status, 200)
        self.assertEqual(listing["items"][0]["id"], created["id"])

        status, updated = self.request(
            "PUT",
            f"/api/workflows/{created['id']}",
            {
                "name": "API workflow v2",
                "parallelism": 1,
                "nodes": [{"key": "wait", "kind": "delay", "delay_seconds": 30}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["version"], 2)

        status, run = self.request(
            "POST",
            f"/api/workflows/{created['id']}/runs",
            {"owner_approved": True},
        )
        self.assertEqual(status, 202)

        status, detail = self.request("GET", f"/api/workflow-runs/{run['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["workflow_id"], created["id"])

        status, events = self.request(
            "GET",
            f"/api/workflow-runs/{run['id']}/events?after=0",
        )
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(events["items"]), 1)

        status, cancelled = self.request(
            "POST",
            f"/api/workflow-runs/{run['id']}/cancel",
            {"reason": "API test"},
        )
        self.assertEqual(status, 202)
        self.assertIn(cancelled["status"], {"cancelling", "cancelled"})

        self.wait_for_run_status(run["id"], {"cancelled"})
        status, results = self.request(
            "GET",
            f"/api/workflow-runs/{run['id']}/results",
        )
        self.assertEqual(status, 200)
        self.assertEqual(results["status"], "cancelled")

        status, retried = self.request(
            "POST",
            f"/api/workflow-runs/{run['id']}/retry",
            {"owner_approved": True},
        )
        self.assertEqual(status, 202)
        self.assertEqual(retried["retry_of_run_id"], run["id"])
        self.app.workflow_orchestrator.cancel(retried["id"], reason="cleanup")

    def test_cancel_is_available_while_kill_switch_is_active(self) -> None:
        _, created = self.create_workflow()
        _, run = self.request(
            "POST",
            f"/api/workflows/{created['id']}/runs",
            {"owner_approved": True},
        )
        self.app.control.activate(reason="API emergency", source="test")
        response = urllib.request.urlopen(f"{self.base_url}/", timeout=10)
        self.cookie = response.headers["Set-Cookie"].split(";", 1)[0]

        status, payload = self.request(
            "POST",
            f"/api/workflow-runs/{run['id']}/cancel",
            {"reason": "owner confirms stop"},
        )

        self.assertEqual(status, 202)
        self.assertIn(payload["status"], {"cancelling", "cancelled"})
        self.assertEqual(
            self.wait_for_run_status(run["id"], {"cancelled"})["status"],
            "cancelled",
        )

    def test_owner_can_decide_a_waiting_human_checkpoint(self) -> None:
        status, created = self.request(
            "POST",
            "/api/workflows",
            {
                "name": "Approval API workflow",
                "nodes": [
                    {
                        "key": "gate",
                        "kind": "approval",
                        "prompt": "Continue?",
                    }
                ],
            },
        )
        self.assertEqual(status, 201)
        _, run = self.request(
            "POST",
            f"/api/workflows/{created['id']}/runs",
            {"owner_approved": False},
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, detail = self.request("GET", f"/api/workflow-runs/{run['id']}")
            if detail["nodes"][0]["status"] == "waiting_approval":
                break
            time.sleep(0.05)
        else:
            self.fail("approval node did not enter waiting state")

        status, decided = self.request(
            "POST",
            f"/api/workflow-runs/{run['id']}/nodes/{detail['nodes'][0]['id']}/decision",
            {"approved": True, "note": "API owner approval"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(decided["status"], "succeeded")
        self.assertTrue(decided["result"]["approved"])

    def test_invalid_workflow_states_return_conflict(self) -> None:
        _, created = self.create_workflow()
        self.request("DELETE", f"/api/workflows/{created['id']}")

        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request(
                "POST",
                f"/api/workflows/{created['id']}/runs",
                {"owner_approved": True},
            )
        self.assertEqual(captured.exception.code, 409)
        self.assertEqual(self.error_response(captured.exception)["code"], "workflow_state_conflict")

        _, active = self.create_workflow()
        _, run = self.request(
            "POST",
            f"/api/workflows/{active['id']}/runs",
            {"owner_approved": True},
        )
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request(
                "POST",
                f"/api/workflow-runs/{run['id']}/retry",
                {"owner_approved": True},
            )
        self.assertEqual(captured.exception.code, 409)
        self.assertEqual(self.error_response(captured.exception)["code"], "workflow_state_conflict")
        self.app.workflow_orchestrator.cancel(run["id"], reason="cleanup")

        _, approval = self.request(
            "POST",
            "/api/workflows",
            {
                "name": "Duplicate approval API workflow",
                "nodes": [{"key": "gate", "kind": "approval", "prompt": "Continue?"}],
            },
        )
        _, approval_run = self.request(
            "POST",
            f"/api/workflows/{approval['id']}/runs",
            {"owner_approved": False},
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, detail = self.request("GET", f"/api/workflow-runs/{approval_run['id']}")
            if detail["nodes"][0]["status"] == "waiting_approval":
                break
            time.sleep(0.05)
        else:
            self.fail("approval node did not enter waiting state")
        decision_path = (
            f"/api/workflow-runs/{approval_run['id']}/nodes/"
            f"{detail['nodes'][0]['id']}/decision"
        )
        self.request("POST", decision_path, {"approved": True, "note": "first decision"})

        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request("POST", decision_path, {"approved": False, "note": "duplicate"})
        self.assertEqual(captured.exception.code, 409)
        self.assertEqual(self.error_response(captured.exception)["code"], "workflow_state_conflict")


if __name__ == "__main__":
    unittest.main()
