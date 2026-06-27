from __future__ import annotations

import ast
import json
import math
import operator
import re
import statistics
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
        if any(
            marker in lowered
            for marker in (
                "math", "solve", "calculate", "algebra", "calculus", "equation",
                "quadratic", "percent", "%", "average", "mean", "median",
                "sqrt", "square root", "factorial", "standard deviation",
            )
        ):
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
        # Intrinsic structured solvers (no model needed), most-specific first.
        # Each returns "" when it does not apply, so we never emit a wrong answer.
        for solver in (
            self._solve_quadratic_equation,
            self._solve_linear_equation,
            self._solve_percentage,
            self._solve_statistics,
        ):
            result = solver(instruction)
            if result:
                return result
        expression = self._extract_arithmetic_expression(instruction)
        if expression:
            try:
                value = self._safe_eval(expression)
                return f"Calculation:\n{expression} = {value:g}\n\nCheck the expression and units before using the result."
            except (ValueError, ZeroDivisionError, OverflowError):
                pass
        return (
            "Math approach:\n"
            "1. Identify the knowns, unknowns, and target variable.\n"
            "2. Write the equation before plugging in numbers.\n"
            "3. Solve step by step and verify by substitution.\n\n"
            f"Problem restated: {instruction}"
        )

    _EQUATION_LEAD_IN = (
        r"^(solve|calculate|compute|evaluate|find|simplify|what\s+is|what's|whats|"
        r"the\s+equation|the\s+roots\s+of|roots\s+of|for)\s+"
    )

    def _solve_quadratic_equation(self, instruction: str) -> str:
        cleaned = re.sub(self._EQUATION_LEAD_IN, "", instruction.strip(), flags=re.IGNORECASE)
        text = cleaned.lower().replace(" ", "").replace("²", "^2").replace("**", "^")
        if "=" not in text or ("x^2" not in text and "x2" not in text):
            return ""
        lhs, rhs = text.split("=", 1)
        # Both sides must be pure polynomial-in-x expressions, else bail (no guess).
        equation_chars = re.compile(r"[0-9x^.+\-*/]+")
        if not (equation_chars.fullmatch(lhs) and equation_chars.fullmatch(rhs)):
            return ""
        try:
            a_l, b_l, c_l = self._poly_coeffs(lhs)
            a_r, b_r, c_r = self._poly_coeffs(rhs)
        except ValueError:
            return ""
        a, b, c = a_l - a_r, b_l - b_r, c_l - c_r
        if a == 0:
            return ""  # not quadratic; let the linear solver try
        disc = b * b - 4 * a * c
        header = (
            "Solve the quadratic equation:\n"
            f"{a:g}x^2 + {b:g}x + {c:g} = 0\n"
            f"discriminant = b^2 - 4ac = {b:g}^2 - 4·{a:g}·{c:g} = {disc:g}\n"
        )
        if disc > 0:
            root1 = (-b + math.sqrt(disc)) / (2 * a)
            root2 = (-b - math.sqrt(disc)) / (2 * a)
            return header + (
                f"x = (-b ± √discriminant) / 2a\n"
                f"x = {root1:g}  or  x = {root2:g}\n\n"
                f"Answer: x = {root1:g} or x = {root2:g}"
            )
        if disc == 0:
            root = -b / (2 * a)
            return header + f"x = -b / 2a = {root:g}\n\nAnswer: x = {root:g} (double root)"
        real = -b / (2 * a)
        imag = math.sqrt(-disc) / (2 * a)
        return header + (
            "The discriminant is negative, so the roots are complex:\n"
            f"x = {real:g} ± {imag:g}i\n\n"
            f"Answer: x = {real:g} + {imag:g}i or x = {real:g} - {imag:g}i"
        )

    @staticmethod
    def _poly_coeffs(side: str) -> tuple[float, float, float]:
        """Parse a polynomial in x into (a, b, c) for x^2, x, and constant terms.
        Raises ValueError on any term that is not a clean x^2 / x / numeric term."""
        a = b = c = 0.0
        normalized = side.replace("-", "+-")
        for term in normalized.split("+"):
            if not term:
                continue
            if "x^2" in term or "x2" in term:
                a += KnowledgeWorkService._coefficient_value(term.replace("x^2", "").replace("x2", ""))
            elif "x" in term:
                b += KnowledgeWorkService._coefficient_value(term.replace("x", ""))
            else:
                c += float(term)  # ValueError here aborts the parse -> no false answer
        return a, b, c

    def _solve_percentage(self, instruction: str) -> str:
        lowered = instruction.lower()
        of_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s*of\s*(\d+(?:\.\d+)?)", lowered)
        if of_match:
            percent = float(of_match.group(1))
            whole = float(of_match.group(2))
            value = whole * percent / 100
            return (
                f"{percent:g}% of {whole:g}:\n"
                f"{whole:g} × {percent:g}/100 = {value:g}\n\n"
                f"Answer: {value:g}"
            )
        is_what_match = re.search(
            r"(\d+(?:\.\d+)?)\s*is\s*what\s*percent\s*of\s*(\d+(?:\.\d+)?)", lowered
        )
        if is_what_match:
            part = float(is_what_match.group(1))
            whole = float(is_what_match.group(2))
            if whole == 0:
                return ""
            percent = part / whole * 100
            return (
                f"{part:g} is what percent of {whole:g}:\n"
                f"({part:g} / {whole:g}) × 100 = {percent:g}%\n\n"
                f"Answer: {percent:g}%"
            )
        return ""

    def _solve_statistics(self, instruction: str) -> str:
        lowered = instruction.lower()
        if "median" in lowered:
            stat = "median"
        elif "standard deviation" in lowered or "std dev" in lowered:
            stat = "stdev"
        elif "average" in lowered or "mean" in lowered:
            stat = "mean"
        else:
            return ""
        numbers = [float(token) for token in re.findall(r"-?\d+(?:\.\d+)?", lowered)]
        if len(numbers) < 2:
            return ""
        if stat == "median":
            value, label = statistics.median(numbers), "Median"
        elif stat == "stdev":
            value, label = statistics.pstdev(numbers), "Population standard deviation"
        else:
            value, label = statistics.fmean(numbers), "Mean (average)"
        listed = ", ".join(f"{n:g}" for n in numbers)
        return f"{label} of {listed}:\n{label} = {value:g}\n\nAnswer: {value:g}"

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

    _FUNCTION_PATTERN = r"\b(sqrt|sin|cos|tan|log|ln|log10|exp|abs|factorial|floor|ceil|pow|round)\s*\("

    @staticmethod
    def _extract_arithmetic_expression(instruction: str) -> str:
        text = instruction.strip().rstrip("?.! ")
        text = re.sub(
            r"^(what\s+is|what's|whats|calculate|compute|evaluate|how\s+much\s+is)\s+",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        text = text.replace("×", "*").replace("÷", "/").replace("^", "**")
        if not re.search(r"\d", text):
            return ""
        has_operator = re.search(r"[-+*/%]", text) is not None
        has_function = re.search(KnowledgeWorkService._FUNCTION_PATTERN, text, re.IGNORECASE) is not None
        if not (has_operator or has_function):
            return ""
        # Restrict to a math-only charset so prose never reaches the parser.
        if not re.fullmatch(r"[0-9a-z+\-*/%.,()\s]+", text, re.IGNORECASE):
            return ""
        return text

    def _safe_eval(self, expression: str) -> float:
        operators = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Mod: operator.mod,
            ast.Pow: operator.pow,
            ast.USub: operator.neg,
            ast.UAdd: operator.pos,
        }
        functions = {
            "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
            "log": math.log, "ln": math.log, "log10": math.log10, "exp": math.exp,
            "abs": abs, "floor": math.floor, "ceil": math.ceil,
            "factorial": lambda value: math.factorial(int(value)),
            "pow": pow, "round": round,
        }
        constants = {"pi": math.pi, "e": math.e, "tau": math.tau}

        def evaluate(node: ast.AST) -> float:
            if isinstance(node, ast.Expression):
                return evaluate(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return float(node.value)
            if isinstance(node, ast.BinOp) and type(node.op) in operators:
                return operators[type(node.op)](evaluate(node.left), evaluate(node.right))
            if isinstance(node, ast.UnaryOp) and type(node.op) in operators:
                return operators[type(node.op)](evaluate(node.operand))
            if isinstance(node, ast.Name) and node.id in constants:
                return float(constants[node.id])
            # Only whitelisted function NAMES are callable — no attributes, no
            # arbitrary names, no keywords. There is no code-execution surface.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in functions
                and not node.keywords
            ):
                args = [evaluate(arg) for arg in node.args]
                return float(functions[node.func.id](*args))
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
