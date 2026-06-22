from __future__ import annotations

import unittest

from project_q.lora_evaluator import evaluate_records, extract_tool_id


class LoraEvaluatorTests(unittest.TestCase):
    def test_extract_tool_id_requires_a_known_standalone_identifier(self) -> None:
        tools = ["browser.complete_goal", "windows.open_url"]

        self.assertEqual(
            extract_tool_id("browser.complete_goal", tools),
            "browser.complete_goal",
        )
        self.assertEqual(
            extract_tool_id("Result: `windows.open_url`.", tools),
            "windows.open_url",
        )
        self.assertEqual(extract_tool_id("browser.complete_goal_extra", tools), "")
        self.assertEqual(extract_tool_id("I cannot decide", tools), "")

    def test_evaluate_records_returns_metrics_and_per_prompt_evidence(self) -> None:
        records = [
            {"input": "research this", "expected_tool": "browser.complete_goal"},
            {"input": "open this URL", "expected_tool": "windows.open_url"},
        ]
        outputs = iter(["browser.complete_goal", "knowledge.answer"])

        report = evaluate_records(
            records,
            generate=lambda _prompt: next(outputs),
            tool_ids=["browser.complete_goal", "windows.open_url", "knowledge.answer"],
            clock_values=iter([1.0, 1.1, 2.0, 2.3]),
        )

        self.assertEqual(report["evaluated_prompts"], 2)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["failures"], 1)
        self.assertEqual(report["score"], 0.5)
        self.assertAlmostEqual(report["latency_ms"], 200.0)
        self.assertTrue(report["results"][0]["passed"])
        self.assertFalse(report["results"][1]["passed"])


if __name__ == "__main__":
    unittest.main()
