from __future__ import annotations

import ast
import json
import operator
import re
from typing import Any, Callable
from urllib import error, request


KnowledgeTransport = Callable[[str, bytes, dict[str, str], int], dict[str, Any]]


class KnowledgeWorkService:
    def __init__(self, settings_service, audit_service, transport: KnowledgeTransport | None = None) -> None:
        self.settings_service = settings_service
        self.audit_service = audit_service
        self.transport = transport or self._default_transport

    def answer(self, instruction: str, *, depth: str = "standard", style: str = "") -> dict[str, Any]:
        clean_instruction = instruction.strip()
        domain = self.classify(clean_instruction)
        model_result = None
        if depth != "local-only":
            model_result = self._answer_with_local_model(clean_instruction, domain=domain, depth=depth, style=style)
        if model_result is not None:
            return model_result

        answer = self._fallback_answer(clean_instruction, domain=domain, depth=depth, style=style)
        self.audit_service.log(
            action_type="knowledge_work",
            action_tier=0,
            tool_name="knowledge.answer",
            outcome="completed",
            model="local-structured",
            input_sources=["owner", "local_knowledge_workbench"],
            metadata={"domain": domain, "source": "local-structured"},
        )
        return {
            "domain": domain,
            "answer": answer,
            "source": "local-structured",
            "model_name": "",
            "confidence": 0.72,
            "next_actions": self._next_actions(domain),
        }

    def classify(self, instruction: str) -> str:
        lowered = instruction.lower()
        if any(marker in lowered for marker in ("essay", "write a paper", "write an article", "compose", "draft")):
            return "writing"
        if any(marker in lowered for marker in ("physics", "force", "velocity", "acceleration", "momentum", "energy")):
            return "physics"
        if any(marker in lowered for marker in ("quant", "return", "roi", "portfolio", "volatility", "sharpe")):
            return "quant"
        if any(marker in lowered for marker in ("history", "historical", "civilization", "empire", "war", "revolution")):
            return "history"
        if any(marker in lowered for marker in ("math", "solve", "calculate", "algebra", "calculus", "equation")):
            return "math"
        if any(marker in lowered for marker in ("chemistry", "biology", "science", "experiment")):
            return "science"
        return "general"

    def _answer_with_local_model(
        self,
        instruction: str,
        *,
        domain: str,
        depth: str,
        style: str,
    ) -> dict[str, Any] | None:
        settings = self.settings_service.get_all()
        if not settings.get("provider_enabled") or settings.get("provider_type", "ollama") != "ollama":
            return None

        model_name = self._select_model(settings, domain)
        base_url = str(settings.get("model_base_url", "")).strip()
        if not model_name or not base_url:
            return None

        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": self._system_prompt(domain=domain, depth=depth, style=style)},
                {"role": "user", "content": instruction},
            ],
            "stream": False,
            "think": False,
        }
        try:
            raw = self.transport(
                base_url,
                json.dumps(payload).encode("utf-8"),
                {"Content-Type": "application/json"},
                max(int(settings.get("provider_timeout_seconds", 120)), 120),
            )
            answer = self._extract_ollama_text(raw).strip()
        except Exception as exc:  # noqa: BLE001
            self.audit_service.log(
                action_type="knowledge_work",
                action_tier=0,
                tool_name="knowledge.answer",
                outcome="failed",
                model=model_name,
                error=str(exc),
                input_sources=["owner", "ollama"],
                metadata={"domain": domain, "fallback": "local-structured"},
            )
            return None

        if not answer:
            return None
        answer = self._strip_thinking(answer)
        self.audit_service.log(
            action_type="knowledge_work",
            action_tier=0,
            tool_name="knowledge.answer",
            outcome="completed",
            model=model_name,
            input_sources=["owner", "ollama"],
            metadata={"domain": domain, "source": "local-model"},
        )
        return {
            "domain": domain,
            "answer": answer,
            "source": "local-model",
            "model_name": model_name,
            "confidence": 0.86,
            "next_actions": self._next_actions(domain),
        }

    def _fallback_answer(self, instruction: str, *, domain: str, depth: str, style: str) -> str:
        del depth, style
        if domain == "math":
            return self._math_answer(instruction)
        if domain == "physics":
            return self._physics_answer(instruction)
        if domain == "quant":
            return self._quant_answer(instruction)
        if domain == "history":
            return self._history_answer(instruction)
        if domain == "writing":
            return self._writing_answer(instruction)
        if domain == "science":
            return self._science_answer(instruction)
        return self._general_answer(instruction)

    def _math_answer(self, instruction: str) -> str:
        equation = self._solve_linear_equation(instruction)
        if equation:
            return equation
        expression = self._extract_arithmetic_expression(instruction)
        if expression:
            try:
                value = self._safe_eval(expression)
                return f"Calculation:\n{expression} = {value:g}\n\nCheck the expression and units before using the result."
            except ValueError:
                pass
        return (
            "Math approach:\n"
            "1. Identify the knowns, unknowns, and target variable.\n"
            "2. Write the equation before plugging in numbers.\n"
            "3. Solve step by step and verify by substitution.\n\n"
            f"Problem restated: {instruction}"
        )

    def _physics_answer(self, instruction: str) -> str:
        lowered = instruction.lower()
        mass_match = re.search(r"(\d+(?:\.\d+)?)\s*kg", lowered)
        acceleration_match = re.search(r"(\d+(?:\.\d+)?)\s*m\s*/?\s*s(?:\^2|\*\*2|2)?", lowered)
        if mass_match and acceleration_match and any(word in lowered for word in ("force", "needed", "newton")):
            mass = float(mass_match.group(1))
            acceleration = float(acceleration_match.group(1))
            force = mass * acceleration
            return (
                "Use Newton's second law:\n"
                "F = m x a\n"
                f"F = {mass:g} kg x {acceleration:g} m/s^2\n"
                f"F = {force:g} N\n\n"
                f"Answer: {force:g} N"
            )
        return (
            "Physics solution frame:\n"
            "1. Draw or describe the system.\n"
            "2. List known quantities with units.\n"
            "3. Choose the governing equation, such as F = m x a, conservation of energy, or conservation of momentum.\n"
            "4. Substitute values, solve, and check units.\n\n"
            f"Problem restated: {instruction}"
        )

    def _quant_answer(self, instruction: str) -> str:
        lowered = instruction.lower()
        move_match = re.search(r"from\s+(\d+(?:\.\d+)?)\s+to\s+(\d+(?:\.\d+)?)", lowered)
        if move_match:
            start = float(move_match.group(1))
            end = float(move_match.group(2))
            if start != 0:
                percentage = ((end - start) / start) * 100
                return (
                    "Return calculation:\n"
                    "return = (ending price - starting price) / starting price\n"
                    f"return = ({end:g} - {start:g}) / {start:g}\n"
                    f"return = {percentage:.2f}%\n\n"
                    f"Answer: {percentage:.2f}%"
                )
        roi_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*roi\s+(?:on|for)\s+(\d+(?:\.\d+)?)", lowered)
        if roi_match:
            roi = float(roi_match.group(1))
            principal = float(roi_match.group(2))
            gain = principal * roi / 100
            return f"ROI gain = principal x ROI = {principal:g} x {roi / 100:g} = {gain:g}."
        return (
            "Quant analysis frame:\n"
            "1. Define the metric: return, volatility, drawdown, Sharpe, exposure, or expected value.\n"
            "2. State assumptions and time horizon.\n"
            "3. Calculate the metric and sanity-check the magnitude.\n\n"
            f"Prompt restated: {instruction}"
        )

    @staticmethod
    def _history_answer(instruction: str) -> str:
        return (
            "Context:\n"
            f"The key is to place the topic in its time period, power structure, technology, economy, and culture: {instruction}\n\n"
            "Why it mattered:\n"
            "History answers should connect cause, event, and consequence. Focus on what changed, who benefited, who lost power, and what long-term effects followed.\n\n"
            "Suggested structure:\n"
            "1. Brief background.\n"
            "2. Main causes or forces.\n"
            "3. Important turning points.\n"
            "4. Consequences and modern relevance."
        )

    @staticmethod
    def _writing_answer(instruction: str) -> str:
        return (
            "Thesis:\n"
            f"A strong essay on this prompt should make one clear argument instead of listing facts: {instruction}\n\n"
            "Outline:\n"
            "1. Introduction with context and thesis.\n"
            "2. Body paragraph 1: first major cause or argument.\n"
            "3. Body paragraph 2: second major cause or argument.\n"
            "4. Body paragraph 3: counterpoint, complexity, or consequence.\n"
            "5. Conclusion that explains why the topic matters.\n\n"
            "Draft opening:\n"
            "The subject is important because it shows how multiple forces can combine into a larger turning point. "
            "A strong answer should explain causes, connect them logically, and show the consequences rather than simply summarize events."
        )

    @staticmethod
    def _science_answer(instruction: str) -> str:
        return (
            "Science problem-solving frame:\n"
            "1. Define the system or phenomenon.\n"
            "2. List variables, units, and assumptions.\n"
            "3. Apply the relevant principle or equation.\n"
            "4. Explain the result in plain language.\n\n"
            f"Prompt restated: {instruction}"
        )

    @staticmethod
    def _general_answer(instruction: str) -> str:
        return (
            "Problem-solving frame:\n"
            "1. Clarify the goal.\n"
            "2. Break it into smaller parts.\n"
            "3. Solve the highest-impact part first.\n"
            "4. Verify the result and list next steps.\n\n"
            f"Prompt restated: {instruction}"
        )

    def _solve_linear_equation(self, instruction: str) -> str:
        compact = instruction.replace(" ", "")
        match = re.search(
            r"([+-]?(?:\d+(?:\.\d+)?)?)\*?([a-zA-Z])([+-]\d+(?:\.\d+)?)?=([+-]?\d+(?:\.\d+)?)",
            compact,
        )
        if not match:
            return ""
        coefficient_text, variable, intercept_text, rhs_text = match.groups()
        coefficient = self._coefficient_value(coefficient_text)
        intercept = float(intercept_text or 0)
        rhs = float(rhs_text)
        if coefficient == 0:
            return "This linear equation has a zero coefficient on the variable, so it cannot be solved for that variable in the usual way."
        value = (rhs - intercept) / coefficient
        return (
            "Solve the linear equation:\n"
            f"{coefficient:g}{variable} + {intercept:g} = {rhs:g}\n"
            f"{coefficient:g}{variable} = {rhs:g} - {intercept:g}\n"
            f"{coefficient:g}{variable} = {rhs - intercept:g}\n"
            f"{variable} = {(rhs - intercept):g} / {coefficient:g}\n"
            f"{variable} = {value:g}\n\n"
            f"Answer: {variable} = {value:g}"
        )

    @staticmethod
    def _coefficient_value(text: str) -> float:
        if text in {"", "+"}:
            return 1.0
        if text == "-":
            return -1.0
        return float(text)

    @staticmethod
    def _extract_arithmetic_expression(instruction: str) -> str:
        match = re.search(r"([-+*/().\d\s]+)", instruction)
        if not match:
            return ""
        expression = match.group(1).strip()
        if not re.search(r"\d", expression) or not re.search(r"[-+*/]", expression):
            return ""
        return expression

    def _safe_eval(self, expression: str) -> float:
        operators = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Pow: operator.pow,
            ast.USub: operator.neg,
            ast.UAdd: operator.pos,
        }

        def evaluate(node: ast.AST) -> float:
            if isinstance(node, ast.Expression):
                return evaluate(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return float(node.value)
            if isinstance(node, ast.BinOp) and type(node.op) in operators:
                return operators[type(node.op)](evaluate(node.left), evaluate(node.right))
            if isinstance(node, ast.UnaryOp) and type(node.op) in operators:
                return operators[type(node.op)](evaluate(node.operand))
            raise ValueError("unsupported expression")

        parsed = ast.parse(expression, mode="eval")
        return evaluate(parsed)

    @staticmethod
    def _select_model(settings: dict[str, Any], domain: str) -> str:
        if domain in {"math", "physics", "quant", "science"}:
            return settings.get("ollama_reasoning_model") or settings.get("ollama_general_model") or settings.get("model_name") or ""
        return settings.get("ollama_general_model") or settings.get("model_name") or ""

    @staticmethod
    def _system_prompt(*, domain: str, depth: str, style: str) -> str:
        return (
            "You are Project Q's Knowledge Workbench. Provide a direct, useful answer for the owner. "
            "Handle typos, infer the likely intent, show formulas or reasoning when helpful, and be practical. "
            f"Domain: {domain}. Depth: {depth}. Style: {style or 'clear and structured'}. "
            "For math, physics, and quant, show steps and units. For writing, produce polished structure and draftable text. "
            "For history, explain context, causes, consequences, and significance."
        )

    @staticmethod
    def _next_actions(domain: str) -> list[str]:
        if domain == "writing":
            return ["Ask Project Q to turn this into a full draft or save it to a document."]
        if domain in {"math", "physics", "quant", "science"}:
            return ["Ask Project Q to verify with another method or generate practice problems."]
        return ["Ask Project Q to expand, simplify, or turn the answer into notes."]

    @staticmethod
    def _extract_ollama_text(response_payload: dict[str, Any]) -> str:
        message = response_payload.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(response_payload.get("response"), str):
            return response_payload["response"]
        raise ValueError("No model text found in Ollama response")

    @staticmethod
    def _strip_thinking(answer: str) -> str:
        return re.sub(r"<think>.*?</think>", "", answer, flags=re.IGNORECASE | re.DOTALL).strip()

    @staticmethod
    def _default_transport(url: str, body: bytes, headers: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Provider request failed: {exc.code} {details}") from exc
