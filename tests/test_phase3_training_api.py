from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.server import ProjectQHandler


class TrainingApiTests(unittest.TestCase):
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
        handler = type("TrainingApiHandler", (ProjectQHandler,), {"app": self.app})
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

    def _trained_job(self) -> dict:
        prepared = self.app.training.prepare_lora_job(reason="API test")
        job_dir = Path(prepared["job_dir"])
        config = json.loads((job_dir / "config.json").read_text(encoding="utf-8"))
        adapter_dir = Path(config["output_adapter_dir"])
        adapter_dir.mkdir(parents=True, exist_ok=True)
        (adapter_dir / "adapter_config.json").write_text("{}", encoding="utf-8")
        (adapter_dir / "adapter_model.safetensors").write_bytes(b"adapter")
        return prepared

    def test_training_job_get_endpoints_require_owner_session(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request("GET", "/api/training/jobs", owner=False)
        self.assertEqual(captured.exception.code, 401)

    def test_job_list_detail_audit_and_untrusted_api_evaluation_cannot_promote(self) -> None:
        prepared = self._trained_job()
        job_id = prepared["job_id"]

        status, listing = self.request("GET", "/api/training/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(listing["items"][0]["job_id"], job_id)

        status, detail = self.request("GET", f"/api/training/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["status"], "trained")

        status, audit = self.request("POST", f"/api/training/jobs/{job_id}/audit", {})
        self.assertEqual(status, 200)
        self.assertTrue(audit["artifact"]["valid"])

        status, report = self.request(
            "POST",
            f"/api/training/jobs/{job_id}/evaluation",
            {
                "base_metrics": {
                    "score": 0.7,
                    "latency_ms": 100,
                    "failures": 2,
                    "evaluated_prompts": 10,
                },
                "adapter_metrics": {
                    "score": 0.9,
                    "latency_ms": 105,
                    "failures": 1,
                    "evaluated_prompts": 10,
                },
                "holdout_prompts": ["unseen alpha", "unseen beta"],
                "minimum_score": 0.85,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(report["promotion"]["approved"])
        self.assertFalse(report["metadata"]["trusted_evaluator"])

        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request(
                "POST",
                f"/api/training/jobs/{job_id}/promote",
                {"owner_confirmed": True},
            )
        self.assertEqual(captured.exception.code, 403)

    def test_api_can_promote_and_rollback_after_trusted_server_evaluation(self) -> None:
        prepared = self._trained_job()
        job_id = prepared["job_id"]
        self.app.training.record_lora_evaluation(
            job_id,
            base_metrics={"score": 0.7, "latency_ms": 100, "failures": 2, "evaluated_prompts": 10},
            adapter_metrics={"score": 0.9, "latency_ms": 105, "failures": 1, "evaluated_prompts": 10},
            holdout_prompts=["trusted unseen alpha", "trusted unseen beta"],
        )

        status, promoted = self.request(
            "POST",
            f"/api/training/jobs/{job_id}/promote",
            {"owner_confirmed": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(promoted["status"], "promoted")

        status, rolled_back = self.request(
            "POST",
            f"/api/training/jobs/{job_id}/rollback",
            {"owner_confirmed": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(rolled_back["status"], "rolled_back")

    def test_rollback_remains_available_during_kill_switch(self) -> None:
        prepared = self._trained_job()
        job_id = prepared["job_id"]
        self.app.training.record_lora_evaluation(
            job_id,
            base_metrics={"score": 0.7, "latency_ms": 100},
            adapter_metrics={"score": 0.9, "latency_ms": 100},
            holdout_prompts=["unseen holdout"],
            minimum_score=0.85,
        )
        self.app.training.promote_lora_job(job_id, owner_confirmed=True)
        self.app.control.activate(reason="test", source="test")
        response = urllib.request.urlopen(f"{self.base_url}/", timeout=10)
        self.cookie = response.headers["Set-Cookie"].split(";", 1)[0]

        status, result = self.request(
            "POST",
            f"/api/training/jobs/{job_id}/rollback",
            {"owner_confirmed": True},
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
