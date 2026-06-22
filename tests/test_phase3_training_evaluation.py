from __future__ import annotations

import importlib
import json
import math
import unittest
from datetime import datetime, timezone
from pathlib import Path


try:
    evaluation = importlib.import_module("project_q.services.training_evaluation")
except ModuleNotFoundError:
    evaluation = None


class TrainingEvaluationTests(unittest.TestCase):
    def require(self, name: str):
        self.assertIsNotNone(evaluation, "training_evaluation module is missing")
        value = getattr(evaluation, name, None)
        self.assertIsNotNone(value, f"training_evaluation.{name} is missing")
        return value

    def test_canonical_prompt_normalization_handles_unicode_case_punctuation_and_space(self) -> None:
        canonicalize_prompt = self.require("canonicalize_prompt")
        normalize_prompt = self.require("normalize_prompt")

        prompt = "  ＯＰＥＮ\tBrowser—NOW!\n"

        self.assertEqual(canonicalize_prompt(prompt), "open browser now")
        self.assertEqual(normalize_prompt(prompt), "open browser now")
        self.assertEqual(canonicalize_prompt("!!!"), "")

    def test_leakage_detection_distinguishes_exact_and_normalized_matches(self) -> None:
        detect_prompt_leakage = self.require("detect_prompt_leakage")

        report = detect_prompt_leakage(
            ["Open browser", "Generate a website!"],
            ["Open browser", "  generate a WEBSITE  ", "Summarize this file"],
        )

        self.assertTrue(report.has_leakage)
        self.assertFalse(report.is_clean)
        self.assertEqual(len(report.exact_matches), 1)
        self.assertEqual(len(report.normalized_matches), 1)
        self.assertEqual(report.exact_matches[0].match_type, "exact")
        self.assertEqual(report.normalized_matches[0].match_type, "normalized")
        self.assertEqual(report.normalized_matches[0].train_index, 1)
        self.assertEqual(report.normalized_matches[0].holdout_index, 1)

    def test_leakage_detection_reports_clean_disjoint_prompts(self) -> None:
        detect_prompt_leakage = self.require("detect_prompt_leakage")

        report = detect_prompt_leakage(
            ["Open browser", "Generate a website"],
            ["Summarize this file", "Analyze this spreadsheet"],
        )

        self.assertTrue(report.is_clean)
        self.assertFalse(report.has_leakage)
        self.assertEqual(report.matches, ())

    def test_deterministic_split_is_repeatable_and_keeps_normalized_duplicates_together(self) -> None:
        canonicalize_prompt = self.require("canonicalize_prompt")
        deterministic_disjoint_split = self.require("deterministic_disjoint_split")
        records = [
            {"input": "Alpha!", "id": 1},
            {"input": " alpha ", "id": 2},
            {"input": "Bravo", "id": 3},
            {"input": "Charlie", "id": 4},
            {"input": "Delta", "id": 5},
            {"input": "Echo", "id": 6},
            {"input": "Foxtrot", "id": 7},
            {"input": "Golf", "id": 8},
            {"input": "Hotel", "id": 9},
            {"input": "India", "id": 10},
        ]

        first = deterministic_disjoint_split(
            records,
            validation_fraction=0.2,
            holdout_fraction=0.2,
            seed="phase-3",
        )
        second = deterministic_disjoint_split(
            records,
            validation_fraction=0.2,
            holdout_fraction=0.2,
            seed="phase-3",
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first.train) + len(first.validation) + len(first.holdout), len(records))
        split_prompts = [
            {canonicalize_prompt(item["input"]) for item in split}
            for split in (first.train, first.validation, first.holdout)
        ]
        self.assertTrue(split_prompts[0].isdisjoint(split_prompts[1]))
        self.assertTrue(split_prompts[0].isdisjoint(split_prompts[2]))
        self.assertTrue(split_prompts[1].isdisjoint(split_prompts[2]))
        alpha_locations = [
            any(canonicalize_prompt(item["input"]) == "alpha" for item in split)
            for split in (first.train, first.validation, first.holdout)
        ]
        self.assertEqual(sum(alpha_locations), 1)

    def test_deterministic_split_validates_fractions_and_prompt_values(self) -> None:
        deterministic_disjoint_split = self.require("deterministic_disjoint_split")

        with self.assertRaises(ValueError):
            deterministic_disjoint_split(
                [{"input": "one"}],
                validation_fraction=0.5,
                holdout_fraction=0.5,
            )
        with self.assertRaises(ValueError):
            deterministic_disjoint_split([{"missing": "prompt"}])
        with self.assertRaises(ValueError):
            deterministic_disjoint_split([{"input": "!!!"}])

    def test_result_comparison_reports_score_latency_and_failure_deltas(self) -> None:
        BaseEvaluationResult = self.require("BaseEvaluationResult")
        AdapterEvaluationResult = self.require("AdapterEvaluationResult")
        compare_evaluation_results = self.require("compare_evaluation_results")
        base = BaseEvaluationResult(
            score=0.80,
            latency_ms=120.0,
            failures=2,
            evaluated_prompts=20,
        )
        adapter = AdapterEvaluationResult(
            score=0.90,
            latency_ms=135.5,
            failures=1,
            evaluated_prompts=20,
        )

        comparison = compare_evaluation_results(base, adapter)

        self.assertAlmostEqual(comparison.score_delta, 0.10)
        self.assertAlmostEqual(comparison.latency_delta_ms, 15.5)
        self.assertEqual(comparison.failure_delta, -1)
        self.assertTrue(comparison.score_improved)
        self.assertTrue(comparison.failures_improved)
        self.assertFalse(comparison.latency_improved)

    def test_evaluation_result_rejects_invalid_metrics(self) -> None:
        BaseEvaluationResult = self.require("BaseEvaluationResult")

        for kwargs in (
            {"score": -0.01, "latency_ms": 1.0},
            {"score": 1.01, "latency_ms": 1.0},
            {"score": 0.5, "latency_ms": -1.0},
            {"score": 0.5, "latency_ms": 1.0, "failures": -1},
            {"score": 0.5, "latency_ms": 1.0, "failures": 2, "evaluated_prompts": 1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BaseEvaluationResult(**kwargs)

    def test_promotion_gate_approves_clean_non_regressing_adapter(self) -> None:
        AdapterEvaluationResult = self.require("AdapterEvaluationResult")
        BaseEvaluationResult = self.require("BaseEvaluationResult")
        PromotionGateConfig = self.require("PromotionGateConfig")
        detect_prompt_leakage = self.require("detect_prompt_leakage")
        evaluate_promotion = self.require("evaluate_promotion")
        base = BaseEvaluationResult(score=0.82, latency_ms=100.0, failures=1)
        adapter = AdapterEvaluationResult(score=0.87, latency_ms=110.0, failures=1)
        leakage = detect_prompt_leakage(["train prompt"], ["holdout prompt"])

        decision = evaluate_promotion(
            base,
            adapter,
            artifact_valid=True,
            leakage=leakage,
            config=PromotionGateConfig(minimum_score=0.85),
        )

        self.assertTrue(decision.promote)
        self.assertEqual(decision.reasons, ())
        self.assertTrue(all(decision.checks.values()))

    def test_promotion_gate_rejects_each_required_safety_condition(self) -> None:
        AdapterEvaluationResult = self.require("AdapterEvaluationResult")
        BaseEvaluationResult = self.require("BaseEvaluationResult")
        PromotionGateConfig = self.require("PromotionGateConfig")
        detect_prompt_leakage = self.require("detect_prompt_leakage")
        evaluate_promotion = self.require("evaluate_promotion")
        base = BaseEvaluationResult(score=0.90, latency_ms=100.0)
        adapter = AdapterEvaluationResult(score=0.80, latency_ms=100.0)
        leaked = detect_prompt_leakage(["same prompt"], [" SAME prompt! "])

        decision = evaluate_promotion(
            base,
            adapter,
            artifact_valid=False,
            leakage=leaked,
            config=PromotionGateConfig(minimum_score=0.85),
        )

        self.assertFalse(decision.promote)
        self.assertFalse(decision.checks["artifact_valid"])
        self.assertFalse(decision.checks["no_leakage"])
        self.assertFalse(decision.checks["minimum_score"])
        self.assertFalse(decision.checks["no_baseline_regression"])
        self.assertEqual(len(decision.reasons), 4)

    def test_promotion_gate_supports_optional_latency_and_failure_budgets(self) -> None:
        AdapterEvaluationResult = self.require("AdapterEvaluationResult")
        BaseEvaluationResult = self.require("BaseEvaluationResult")
        PromotionGateConfig = self.require("PromotionGateConfig")
        detect_prompt_leakage = self.require("detect_prompt_leakage")
        evaluate_promotion = self.require("evaluate_promotion")
        base = BaseEvaluationResult(score=0.90, latency_ms=100.0, failures=0)
        adapter = AdapterEvaluationResult(score=0.89, latency_ms=125.0, failures=2)

        decision = evaluate_promotion(
            base,
            adapter,
            artifact_valid=True,
            leakage=detect_prompt_leakage([], []),
            config=PromotionGateConfig(
                minimum_score=0.85,
                maximum_score_regression=0.02,
                maximum_latency_increase_ms=10.0,
                maximum_failure_increase=1,
            ),
        )

        self.assertFalse(decision.promote)
        self.assertTrue(decision.checks["no_baseline_regression"])
        self.assertFalse(decision.checks["latency_budget"])
        self.assertFalse(decision.checks["failure_budget"])

    def test_report_serialization_is_strictly_json_safe(self) -> None:
        AdapterEvaluationResult = self.require("AdapterEvaluationResult")
        BaseEvaluationResult = self.require("BaseEvaluationResult")
        EvaluationReport = self.require("EvaluationReport")
        PromotionGateConfig = self.require("PromotionGateConfig")
        compare_evaluation_results = self.require("compare_evaluation_results")
        detect_prompt_leakage = self.require("detect_prompt_leakage")
        evaluate_promotion = self.require("evaluate_promotion")
        to_json_safe = self.require("to_json_safe")
        base = BaseEvaluationResult(score=0.75, latency_ms=100.0)
        adapter = AdapterEvaluationResult(score=0.85, latency_ms=95.0)
        comparison = compare_evaluation_results(base, adapter)
        leakage = detect_prompt_leakage([], [])
        decision = evaluate_promotion(
            base,
            adapter,
            artifact_valid=True,
            leakage=leakage,
            config=PromotionGateConfig(minimum_score=0.80),
        )
        report = EvaluationReport(
            comparison=comparison,
            leakage=leakage,
            promotion=decision,
            artifact_valid=True,
            metadata={
                "artifact": Path("adapter/config.json"),
                "created_at": datetime(2026, 6, 14, tzinfo=timezone.utc),
                "tags": {"lora", "phase3"},
                "non_finite": math.inf,
            },
        )

        payload = report.to_dict()
        encoded = report.to_json()

        self.assertEqual(payload, to_json_safe(report))
        self.assertEqual(payload["metadata"]["artifact"], "adapter/config.json")
        self.assertEqual(payload["metadata"]["created_at"], "2026-06-14T00:00:00+00:00")
        self.assertEqual(payload["metadata"]["tags"], ["lora", "phase3"])
        self.assertIsNone(payload["metadata"]["non_finite"])
        self.assertEqual(json.loads(encoded), payload)
        self.assertNotIn("Infinity", encoded)


if __name__ == "__main__":
    unittest.main()
