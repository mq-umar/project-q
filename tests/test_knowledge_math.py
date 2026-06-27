"""Intrinsic math solvers in knowledge.answer (no provider needed)."""
from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig


class KnowledgeMathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            project_name="Knowledge Math Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            port=8913,
        )
        self.app = create_application(config)
        self.tool = self.app.tools.get("knowledge.answer")

    def tearDown(self) -> None:
        sd = getattr(self.app, "shutdown_services", None)
        if callable(sd):
            try:
                sd()
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _answer(self, instruction: str) -> str:
        out = self.tool.execute({"instruction": instruction, "depth": "standard"})
        self.assertEqual(out["domain"], "math", out)
        return out["answer"]

    def test_linear_equation(self) -> None:
        self.assertIn("x = 4", self._answer("solve 2x + 3 = 11"))

    def test_quadratic_two_real_roots(self) -> None:
        answer = self._answer("solve x^2 - 5x + 6 = 0")
        self.assertIn("x = 3", answer)
        self.assertIn("x = 2", answer)

    def test_quadratic_double_root(self) -> None:
        answer = self._answer("solve x^2 - 4x + 4 = 0")
        self.assertIn("x = 2", answer)
        self.assertIn("double root", answer)

    def test_quadratic_complex_roots(self) -> None:
        answer = self._answer("solve x^2 + 1 = 0")
        self.assertIn("i", answer)
        self.assertIn("complex", answer.lower())

    def test_percentage_of(self) -> None:
        self.assertIn("36", self._answer("what is 15% of 240"))

    def test_percentage_is_what(self) -> None:
        self.assertIn("25", self._answer("30 is what percent of 120"))

    def test_mean(self) -> None:
        answer = self._answer("what is the mean of 2, 4, 6")
        self.assertIn("Mean", answer)
        self.assertIn("4", answer)

    def test_median(self) -> None:
        answer = self._answer("median of 1, 3, 5, 7")
        self.assertIn("Median", answer)
        self.assertIn("4", answer)

    def test_sqrt_and_power(self) -> None:
        # sqrt(144)=12, 2^3=8 -> 20
        self.assertIn("20", self._answer("calculate sqrt(144) + 2^3"))

    def test_factorial(self) -> None:
        self.assertIn("120", self._answer("calculate factorial(5)"))

    def test_modulo(self) -> None:
        self.assertIn("2", self._answer("calculate 100 % 7"))

    def test_no_false_answer_falls_back_to_frame(self) -> None:
        answer = self._answer("solve the deep structure of this problem")
        self.assertIn("Math approach", answer)

    def test_no_code_execution_via_expression(self) -> None:
        # Hostile string must not execute; it falls back to the math frame safely.
        answer = self._answer("calculate __import__('os').system('echo hi')")
        self.assertIn("Math approach", answer)


if __name__ == "__main__":
    unittest.main()
