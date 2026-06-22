from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.services.training_evaluation import canonicalize_prompt


class TrainingLifecycleTests(unittest.TestCase):
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

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _user_prompt(record: dict) -> str:
        content = next(
            item["content"]
            for item in record["messages"]
            if item["role"] == "user"
        )
        if "Owner request:" in content:
            content = content.split("Owner request:", 1)[1]
        if "\nTool id:" in content:
            content = content.split("\nTool id:", 1)[0]
        return content.strip()

    def _make_trained_job(self) -> dict:
        prepared = self.app.training.prepare_lora_job(reason="lifecycle test")
        job_dir = Path(prepared["job_dir"])
        adapter_dir = Path(json.loads((job_dir / "config.json").read_text(encoding="utf-8"))["output_adapter_dir"])
        adapter_dir.mkdir(parents=True, exist_ok=True)
        (adapter_dir / "adapter_config.json").write_text(
            json.dumps({"base_model_name_or_path": prepared["base_model"]}),
            encoding="utf-8",
        )
        (adapter_dir / "adapter_model.safetensors").write_bytes(b"valid-test-adapter")
        return prepared

    def test_export_dataset_keeps_training_validation_and_holdout_prompts_disjoint(self) -> None:
        result = self.app.training.export_dataset(reason="disjoint test")
        train = self._read_jsonl(Path(result["sft_dataset_path"]))
        validation = self._read_jsonl(Path(result["validation_dataset_path"]))
        holdout = self._read_jsonl(Path(result["routing_eval_path"]))

        train_prompts = {canonicalize_prompt(self._user_prompt(item)) for item in train}
        validation_prompts = {canonicalize_prompt(self._user_prompt(item)) for item in validation}
        holdout_prompts = {canonicalize_prompt(item["input"]) for item in holdout}

        self.assertTrue(train_prompts.isdisjoint(validation_prompts))
        self.assertTrue(train_prompts.isdisjoint(holdout_prompts))
        self.assertTrue(validation_prompts.isdisjoint(holdout_prompts))
        self.assertGreater(len(train), 40)
        self.assertGreater(len(validation), 0)

    def test_job_discovery_and_audit_validate_artifacts_and_report_prompt_leakage(self) -> None:
        prepared = self._make_trained_job()
        job_dir = Path(prepared["job_dir"])
        sft_path = Path(json.loads((job_dir / "config.json").read_text(encoding="utf-8"))["sft_dataset_path"])
        rows = self._read_jsonl(sft_path)
        rows.append(
            {
                "messages": [
                    {"role": "system", "content": "Route requests."},
                    {"role": "user", "content": "clean holdout prompt"},
                    {"role": "assistant", "content": "knowledge.answer"},
                ],
                "source": "intentional-test-leak",
            }
        )
        self.app.training._write_jsonl(sft_path, rows)
        (job_dir / "eval_prompts.jsonl").write_text(
            json.dumps(
                {
                    "input": " Clean holdout prompt! ",
                    "expected_tool": "knowledge.answer",
                    "must_not_include": "",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        jobs = self.app.training.list_lora_jobs()
        audit = self.app.training.audit_lora_job(prepared["job_id"])

        self.assertEqual(jobs[0]["job_id"], prepared["job_id"])
        self.assertEqual(jobs[0]["status"], "trained")
        self.assertTrue(audit["artifact"]["valid"])
        self.assertTrue(audit["leakage"]["has_leakage"])
        self.assertEqual(audit["leakage"]["normalized_match_count"], 1)

    def test_evaluation_is_persisted_and_failed_gate_cannot_be_promoted(self) -> None:
        prepared = self._make_trained_job()
        report = self.app.training.record_lora_evaluation(
            prepared["job_id"],
            base_metrics={
                "score": 5 / 12,
                "latency_ms": 335.8,
                "failures": 7,
                "evaluated_prompts": 12,
            },
            adapter_metrics={
                "score": 9 / 12,
                "latency_ms": 544.2,
                "failures": 3,
                "evaluated_prompts": 12,
            },
            holdout_prompts=[
                "classify a multi-step browser research request",
                "create a project rather than a website",
                "explain how to prepare training without starting it",
            ],
            minimum_score=0.85,
        )

        self.assertFalse(report["promotion"]["approved"])
        self.assertIn("minimum_score", report["promotion"]["checks"])
        self.assertTrue(Path(prepared["job_dir"], "evaluation_report.json").exists())
        with self.assertRaises(PermissionError):
            self.app.training.promote_lora_job(
                prepared["job_id"],
                owner_confirmed=True,
            )

    def test_evaluation_cannot_lower_the_minimum_promotion_score(self) -> None:
        prepared = self._make_trained_job()

        report = self.app.training.record_lora_evaluation(
            prepared["job_id"],
            base_metrics={"score": 0.2, "latency_ms": 100, "failures": 5, "evaluated_prompts": 10},
            adapter_metrics={"score": 0.4, "latency_ms": 100, "failures": 4, "evaluated_prompts": 10},
            holdout_prompts=["novel gate hardening prompt"],
            minimum_score=0.1,
        )

        self.assertFalse(report["promotion"]["approved"])
        self.assertGreaterEqual(report["promotion"]["config"]["minimum_score"], 0.85)
        self.assertFalse(report["promotion"]["checks"]["minimum_score"])

    def test_clean_approved_evaluation_can_promote_and_rollback_with_owner_confirmation(self) -> None:
        prepared = self._make_trained_job()
        report = self.app.training.record_lora_evaluation(
            prepared["job_id"],
            base_metrics={"score": 0.70, "latency_ms": 100, "failures": 3, "evaluated_prompts": 10},
            adapter_metrics={"score": 0.90, "latency_ms": 110, "failures": 1, "evaluated_prompts": 10},
            holdout_prompts=["novel holdout alpha", "novel holdout beta"],
            minimum_score=0.85,
        )
        self.assertTrue(report["promotion"]["approved"])
        self.assertTrue(report["metadata"]["trusted_evaluator"])

        with self.assertRaises(PermissionError):
            self.app.training.promote_lora_job(prepared["job_id"], owner_confirmed=False)

        promoted = self.app.training.promote_lora_job(
            prepared["job_id"],
            owner_confirmed=True,
        )
        self.assertEqual(promoted["status"], "promoted")
        self.assertEqual(
            json.loads(self.app.training.active_adapter_path.read_text(encoding="utf-8"))["job_id"],
            prepared["job_id"],
        )

        rolled_back = self.app.training.rollback_lora_job(
            prepared["job_id"],
            owner_confirmed=True,
        )
        self.assertEqual(rolled_back["status"], "rolled_back")
        self.assertFalse(self.app.training.active_adapter_path.exists())

    def test_promotion_rejects_adapter_artifact_changed_after_evaluation(self) -> None:
        prepared = self._make_trained_job()
        self.app.training.record_lora_evaluation(
            prepared["job_id"],
            base_metrics={"score": 0.70, "latency_ms": 100, "failures": 3, "evaluated_prompts": 10},
            adapter_metrics={"score": 0.90, "latency_ms": 110, "failures": 1, "evaluated_prompts": 10},
            holdout_prompts=["novel holdout alpha", "novel holdout beta"],
        )
        config = json.loads((Path(prepared["job_dir"]) / "config.json").read_text(encoding="utf-8"))
        (Path(config["output_adapter_dir"]) / "adapter_model.safetensors").write_bytes(
            b"different-adapter"
        )

        with self.assertRaises(PermissionError):
            self.app.training.promote_lora_job(prepared["job_id"], owner_confirmed=True)

    def test_job_ids_and_configured_artifact_paths_cannot_escape_training_root(self) -> None:
        with self.assertRaises(ValueError):
            self.app.training.audit_lora_job("../outside")

        prepared = self.app.training.prepare_lora_job(reason="escape test")
        config_path = Path(prepared["job_dir"]) / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["output_adapter_dir"] = str(Path(prepared["job_dir"]).parent.parent / "outside")
        config_path.write_text(json.dumps(config), encoding="utf-8")

        audit = self.app.training.audit_lora_job(prepared["job_id"])
        self.assertFalse(audit["artifact"]["valid"])
        self.assertIn("outside the job directory", audit["artifact"]["errors"][0])


if __name__ == "__main__":
    unittest.main()
