from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib import error, request
from urllib.parse import quote, urlsplit, urlunsplit

from project_q.models import ReasonerPlan
from project_q.services.intent_normalizer import IntentNormalizer


Transport = Callable[[str, bytes, dict[str, str], int], dict[str, Any]]
GetTransport = Callable[[str, dict[str, str], int], dict[str, Any]]


@dataclass(slots=True)
class ReasonerResult:
    plan: ReasonerPlan
    mode: str
    model_name: str | None
    provider_configured: bool
    warning: str | None = None


class ReasonerService:
    def __init__(
        self,
        settings_service,
        vault_service,
        audit_service,
        transport: Transport | None = None,
        get_transport: GetTransport | None = None,
    ) -> None:
        self.settings_service = settings_service
        self.vault_service = vault_service
        self.audit_service = audit_service
        self.transport = transport or self._default_transport
        self.get_transport = get_transport or self._default_get_transport
        self.intent_normalizer = IntentNormalizer()

    def status(self) -> dict[str, Any]:
        settings = self.settings_service.get_all()
        configured = self._provider_configured(settings)
        provider_type = settings.get("provider_type", "ollama")
        return {
            "mode": self._status_mode(settings, configured),
            "provider_enabled": bool(settings.get("provider_enabled")),
            "provider_configured": configured,
            "model_name": self._status_model_name(settings),
            "provider_type": provider_type,
            "model_routing_enabled": bool(settings.get("ollama_model_routing_enabled", True))
            if provider_type == "ollama"
            else False,
        }

    def plan(self, *, user_message: str, context: dict[str, Any]) -> ReasonerResult:
        settings = self.settings_service.get_all()
        normalization = self.intent_normalizer.normalize(user_message)
        planning_message = normalization.normalized
        planning_context = {
            **context,
            "owner_request_original": user_message,
            "owner_request_normalized": planning_message,
            "intent_corrections": normalization.corrections,
        }
        deterministic_plan = self._deterministic_plan_for_request(user_message=planning_message, context=planning_context)
        if deterministic_plan is not None:
            self.audit_service.log(
                action_type="reasoner_plan",
                action_tier=0,
                tool_name="deterministic_router",
                outcome="completed",
                metadata={
                    "provider_enabled": bool(settings.get("provider_enabled")),
                    "provider_configured": self._provider_configured(settings),
                    "routed_tool_ids": [call.tool_id for call in deterministic_plan.tool_calls],
                    "intent_normalized": normalization.changed,
                    "intent_corrections": normalization.corrections[:12],
                },
                input_sources=["owner", "local_intent_router"],
            )
            return ReasonerResult(
                plan=deterministic_plan,
                mode="deterministic",
                model_name=self._status_model_name(settings) if self._provider_configured(settings) else None,
                provider_configured=self._provider_configured(settings),
            )

        if settings.get("provider_enabled") and self._provider_configured(settings):
            selected_model = self._provider_model_for_request(
                user_message=planning_message,
                context=planning_context,
                settings=settings,
            )
            request_settings = {**settings, "resolved_model_name": selected_model}
            try:
                plan = self._plan_provider(user_message=planning_message, context=planning_context, settings=request_settings)
                if self._should_override_noop_plan(plan=plan, user_message=planning_message):
                    local_plan = self._actionable_fallback_plan(user_message=planning_message, context=planning_context)
                    self.audit_service.log(
                        action_type="reasoner_plan",
                        action_tier=0,
                        tool_name=f"{settings.get('provider_type', 'ollama')}_reasoner",
                        outcome="overridden",
                        model=selected_model or "remote-model",
                        metadata={
                            "provider_type": settings.get("provider_type", "ollama"),
                            "selected_model": selected_model,
                            "reason": "provider_returned_noop_for_actionable_request",
                            "intent_normalized": normalization.changed,
                        },
                        input_sources=["owner", "memory", "tasks", "agents", "routines"],
                    )
                    return ReasonerResult(
                        plan=local_plan,
                        mode="local-fallback",
                        model_name=selected_model,
                        provider_configured=True,
                        warning="Provider returned an empty no-op plan for an actionable request, so Project Q used the local action router.",
                    )
                self.audit_service.log(
                    action_type="reasoner_plan",
                    action_tier=0,
                    tool_name=f"{settings.get('provider_type', 'ollama')}_reasoner",
                    outcome="completed",
                    model=selected_model or "remote-model",
                    metadata={
                        "provider_type": settings.get("provider_type", "ollama"),
                        "selected_model": selected_model,
                        "intent_normalized": normalization.changed,
                        "intent_corrections": normalization.corrections[:12],
                    },
                    input_sources=["owner", "memory", "tasks", "agents", "routines"],
                )
                return ReasonerResult(
                    plan=plan,
                    mode=self._status_mode(settings, True),
                    model_name=selected_model,
                    provider_configured=True,
                )
            except Exception as exc:  # noqa: BLE001
                self.audit_service.log(
                    action_type="reasoner_plan",
                    action_tier=0,
                    tool_name=f"{settings.get('provider_type', 'ollama')}_reasoner",
                    outcome="failed",
                    model=selected_model or "remote-model",
                    error=str(exc),
                )
                local_plan = self._plan_local(user_message=planning_message, context=planning_context)
                return ReasonerResult(
                    plan=local_plan,
                    mode="local-fallback",
                    model_name=selected_model,
                    provider_configured=True,
                    warning=str(exc),
                )

        local_plan = self._plan_local(user_message=planning_message, context=planning_context)
        self.audit_service.log(
            action_type="reasoner_plan",
            action_tier=0,
            tool_name="local_reasoner",
            outcome="completed",
            metadata={
                "provider_enabled": bool(settings.get("provider_enabled")),
                "intent_normalized": normalization.changed,
                "intent_corrections": normalization.corrections[:12],
            },
            input_sources=["owner", "memory", "tasks", "agents", "routines"],
        )
        return ReasonerResult(
            plan=local_plan,
            mode="heuristic",
            model_name=None,
            provider_configured=self._provider_configured(settings),
        )

    def _deterministic_plan_for_request(
        self,
        *,
        user_message: str,
        context: dict[str, Any],
    ) -> ReasonerPlan | None:
        lowered = user_message.lower()
        urls = re.findall(r"https?://[^\s)]+", user_message)
        local_tool_intent = (
            self._wants_spreadsheet_work(lowered)
            or self._wants_file_choice_followup(lowered)
            or self._wants_file_search(lowered)
            or self._wants_capability_plan(lowered)
            or self._wants_training_request(lowered)
            or self._wants_self_diagnostics(lowered)
            or self._wants_website_generation(lowered)
            or self._wants_knowledge_work(lowered)
            or "list files" in lowered
            or "show files" in lowered
            or any(phrase in lowered for phrase in ("list windows", "what windows are open", "show open windows"))
            or any(phrase in lowered for phrase in ("open notepad", "launch notepad"))
            or any(
                phrase in lowered
                for phrase in ("open vscode", "open visual studio code", "launch vscode", "launch visual studio code")
            )
            or self._wants_complex_browser_goal(lowered)
            or self._wants_visible_browser_action(lowered)
            or (
                bool(urls)
                and any(
                    keyword in lowered
                    for keyword in ("research", "summarize", "inspect", "look at", "browse", "analyze")
                )
            )
        )
        if not local_tool_intent:
            return None
        return self._plan_local(user_message=user_message, context=context)

    def _provider_configured(self, settings: dict[str, Any]) -> bool:
        provider_type = settings.get("provider_type", "ollama")
        if provider_type == "openai_responses":
            return bool(settings.get("model_name")) and bool(settings.get("model_secret_name"))
        if provider_type == "ollama":
            return bool(self._status_model_name(settings)) and bool(settings.get("model_base_url"))
        return False

    def _status_mode(self, settings: dict[str, Any], configured: bool) -> str:
        if not settings.get("provider_enabled") or not configured:
            return "heuristic"
        if settings.get("provider_type", "ollama") == "ollama":
            return "local-model"
        return "remote"

    def _plan_provider(self, *, user_message: str, context: dict[str, Any], settings: dict[str, Any]) -> ReasonerPlan:
        provider_type = settings.get("provider_type", "ollama")
        if provider_type == "ollama":
            return self._plan_ollama(user_message=user_message, context=context, settings=settings)
        return self._plan_openai(user_message=user_message, context=context, settings=settings)

    def _plan_openai(self, *, user_message: str, context: dict[str, Any], settings: dict[str, Any]) -> ReasonerPlan:
        secret_name = str(settings["model_secret_name"])
        api_key = self.vault_service.get_secret(secret_name)
        payload = {
            "model": settings.get("resolved_model_name") or settings["model_name"],
            "instructions": self._developer_prompt(),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": self._user_prompt(user_message=user_message, context=context),
                        }
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_object",
                }
            },
        }
        raw = self.transport(
            str(settings["model_base_url"]),
            json.dumps(payload).encode("utf-8"),
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            self._provider_timeout_seconds(settings),
        )
        text = self._extract_text(raw)
        parsed = self._extract_json(text)
        plan = ReasonerPlan.model_validate(parsed)
        return self._normalize_plan_for_request(plan=plan, user_message=user_message)

    def _plan_ollama(self, *, user_message: str, context: dict[str, Any], settings: dict[str, Any]) -> ReasonerPlan:
        payload = {
            "model": settings.get("resolved_model_name") or settings["model_name"],
            "messages": [
                {"role": "system", "content": self._developer_prompt()},
                {"role": "user", "content": self._user_prompt(user_message=user_message, context=context)},
            ],
            "stream": False,
            "format": "json",
            "think": False,
        }
        raw = self.transport(
            str(settings["model_base_url"]),
            json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json"},
            self._provider_timeout_seconds(settings),
        )
        text = self._extract_ollama_text(raw)
        parsed = self._extract_json(text)
        plan = ReasonerPlan.model_validate(parsed)
        return self._normalize_plan_for_request(plan=plan, user_message=user_message)

    def list_available_models(self) -> dict[str, Any]:
        settings = self.settings_service.get_all()
        provider_type = settings.get("provider_type", "ollama")
        if provider_type != "ollama":
            return {
                "provider_type": provider_type,
                "items": [],
                "error": "",
            }

        url = self._ollama_tags_url(str(settings.get("model_base_url", "http://localhost:11434/api/chat")))
        try:
            payload = self.get_transport(url, {"Content-Type": "application/json"}, 5)
            items = [
                {
                    "name": model.get("name") or model.get("model"),
                    "family": (model.get("details") or {}).get("family", ""),
                    "parameter_size": (model.get("details") or {}).get("parameter_size", ""),
                    "quantization_level": (model.get("details") or {}).get("quantization_level", ""),
                }
                for model in payload.get("models", [])
            ]
            return {
                "provider_type": provider_type,
                "items": items,
                "error": "",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "provider_type": provider_type,
                "items": [],
                "error": str(exc),
            }

    def _plan_local(self, *, user_message: str, context: dict[str, Any]) -> ReasonerPlan:
        lowered = user_message.lower()
        urls = re.findall(r"https?://[^\s)]+", user_message)
        payload: dict[str, Any] = {
            "reply": (
                "Project Q processed your request with the built-in local heuristic path. "
                "Enable an advanced model provider in Settings to unlock deeper reasoning, "
                "including free local models through Ollama, richer agents, and reusable automations."
            ),
            "memory_writes": [],
            "task_writes": [],
            "agent_writes": [],
            "routine_writes": [],
            "tool_calls": [],
        }

        if lowered.startswith("remember "):
            payload["reply"] = "I captured that as a memory."
            payload["memory_writes"].append(
                {"text": user_message[9:].strip(), "kind": "semantic", "confidence": 0.92}
            )
        elif lowered.startswith("task:") or lowered.startswith("todo:") or "remind me to" in lowered:
            title = re.sub(r"^(task:|todo:)\s*", "", user_message, flags=re.IGNORECASE).strip()
            title = re.sub(r"^remind me to\s*", "", title, flags=re.IGNORECASE).strip()
            payload["reply"] = f"I turned that into a tracked task: {title[:120] or 'Untitled task'}."
            payload["task_writes"].append(
                {"title": title[:120] or "Untitled task", "description": user_message, "priority": 3}
            )
        elif "spawn" in lowered and "agent" in lowered:
            payload["reply"] = "I prepared a new agent from your request."
            payload["agent_writes"].append(
                {
                    "name": self._agent_name(user_message),
                    "agent_type": self._agent_type(lowered),
                    "goal": user_message,
                    "status": "active",
                    "tools": [
                        "filesystem.list_directory",
                        "browser.inspect_page",
                        "browser.run_actions",
                        "windows.open_url",
                    ],
                }
            )
        elif lowered.startswith("automation:") or lowered.startswith("routine:"):
            goal = re.sub(r"^(automation:|routine:)\s*", "", user_message, flags=re.IGNORECASE).strip()
            payload["reply"] = "I saved that as a reusable routine."
            payload["routine_writes"].append(self._heuristic_routine(goal, lowered, urls))
        elif urls and any(keyword in lowered for keyword in ("research", "summarize", "inspect", "look at", "browse", "analyze")):
            payload["reply"] = "I can inspect that page and bring back the key details."
            payload["tool_calls"].append(
                {
                    "tool_id": "browser.inspect_page",
                    "payload": {"url": urls[0], "include_links": True},
                    "reason": "Fetch live page content instead of guessing.",
                }
            )
        elif self._wants_spreadsheet_work(lowered):
            spreadsheet_path = self._extract_spreadsheet_path(user_message)
            operation = self._extract_spreadsheet_operation(lowered)
            column = self._extract_spreadsheet_column(user_message, operation, spreadsheet_path)
            group_by = self._extract_spreadsheet_group_by(user_message, spreadsheet_path)
            if spreadsheet_path:
                if self._wants_spreadsheet_write(lowered):
                    payload["reply"] = "I can create a Project Q analysis copy of that spreadsheet."
                    payload["tool_calls"].append(
                        {
                            "tool_id": "spreadsheet.write_analysis",
                            "payload": {
                                "path": spreadsheet_path,
                                "operation": operation,
                                "column": column,
                                "group_by": group_by,
                            },
                            "reason": "Create a workbook copy with a readable Project Q analysis sheet.",
                        }
                    )
                else:
                    payload["reply"] = f"I can calculate the {operation} for `{column or 'the spreadsheet'}`."
                    payload["tool_calls"].append(
                        {
                            "tool_id": "spreadsheet.analyze",
                            "payload": {
                                "path": spreadsheet_path,
                                "operation": operation,
                                "column": column,
                                "group_by": group_by,
                            },
                            "reason": "Read the spreadsheet and calculate the requested metric directly.",
                        }
                    )
            else:
                payload["reply"] = "I can help with that spreadsheet, but I need to know which workbook you mean."
                payload["tool_calls"].append(
                    {
                        "tool_id": "filesystem.resolve_file_request",
                        "payload": {
                            "root": self._default_file_search_root(context),
                            "instruction": user_message,
                            "query": "spreadsheet xlsx csv",
                            "action": "reveal",
                            "max_results": 20,
                        },
                        "reason": "Find the relevant workbook before calculating or modifying it.",
                    }
                )
        elif self._wants_file_choice_followup(lowered):
            selection = self._extract_file_choice_selection(lowered)
            mode = "open" if "open" in lowered else "reveal"
            payload["reply"] = f"I can {mode} option {selection} from the previous file results."
            payload["tool_calls"].append(
                {
                    "tool_id": "filesystem.open_file_choice",
                    "payload": {
                        "choice_id": self._extract_recent_file_choice_id(context),
                        "selection": selection,
                        "mode": mode,
                    },
                    "reason": "Use the owner's follow-up selection from the previous ambiguous file search.",
                }
            )
        elif self._wants_file_search(lowered):
            query = self._extract_file_search_query(user_message)
            action = self._file_action_for_request(lowered)
            payload["reply"] = f"I can search your allowed files for: {query}"
            payload["tool_calls"].append(
                {
                    "tool_id": "filesystem.resolve_file_request",
                    "payload": {
                        "root": self._default_file_search_root(context),
                        "instruction": user_message,
                        "query": query,
                        "action": action,
                        "max_results": 50,
                    },
                    "reason": "Resolve a fuzzy file request, ask for confirmation when ambiguous, and open or reveal confident matches.",
                }
            )
        elif self._wants_capability_plan(lowered):
            payload["reply"] = "I can map the practical path toward ChatGPT, Codex, and Claude-like Project Q capability."
            payload["tool_calls"].append(
                {
                    "tool_id": "training.capability_plan",
                    "payload": {},
                    "reason": "Explain the model, tool-use, memory, eval, and fine-tuning loop needed for higher capability.",
                }
            )
        elif self._wants_lora_training_job(lowered):
            payload["reply"] = "I can prepare a local LoRA weight-training job for Project Q."
            payload["tool_calls"].append(
                {
                    "tool_id": "training.prepare_lora_job",
                    "payload": {
                        "reason": "chat",
                        "base_model": "Qwen/Qwen2.5-Coder-1.5B-Instruct",
                        "max_records": 300,
                    },
                    "reason": "Create a concrete local training job with datasets, config, script, and eval prompts.",
                }
            )
        elif self._wants_training_export(lowered):
            payload["reply"] = "I can export Project Q's local training datasets and evaluation assets."
            payload["tool_calls"].append(
                {
                    "tool_id": "training.export_dataset",
                    "payload": {"reason": "chat", "max_records": 200},
                    "reason": "Prepare local datasets and routing evals for future model-weight training.",
                }
            )
        elif self._wants_self_diagnostics(lowered):
            payload["reply"] = "I can run Project Q self-diagnostics and create repair tasks for anything suspicious."
            payload["tool_calls"].append(
                {
                    "tool_id": "diagnostics.run_self_check",
                    "payload": {"source": "chat"},
                    "reason": "Troubleshoot Project Q's own recent failures and run a code-generation probe.",
                }
            )
        elif self._wants_website_generation(lowered):
            payload["reply"] = (
                "I can create a responsive website project for that. This writes files in the workspace, "
                "so approve the chat turn if you want me to generate it now."
            )
            payload["tool_calls"].append(
                {
                    "tool_id": "code.generate_website",
                    "payload": {"instruction": user_message},
                    "reason": "Create a real static website project instead of returning a no-op plan.",
                }
            )
        elif self._wants_project_build(lowered):
            payload["reply"] = (
                "I can generate a real Project Q project scaffold for that. This writes files in the workspace, "
                "so approve the chat turn if you want me to create it now."
            )
            payload["tool_calls"].append(
                {
                    "tool_id": "code.generate_project",
                    "payload": {"instruction": user_message},
                    "reason": "Generate real project files instead of only creating a planning dispatch.",
                }
            )
        elif self._wants_knowledge_work(lowered):
            payload["reply"] = "I can handle that as a knowledge-work task."
            payload["tool_calls"].append(
                {
                    "tool_id": "knowledge.answer",
                    "payload": {"instruction": user_message, "depth": "standard"},
                    "reason": "Use Project Q's writing, academic, math, physics, history, quant, and problem-solving workbench.",
                }
            )
        elif "list files" in lowered or "show files" in lowered:
            payload["reply"] = "I can inspect the current workspace contents."
            payload["tool_calls"].append(
                {
                    "tool_id": "filesystem.list_directory",
                    "payload": {"path": "."},
                    "reason": "Inspect the workspace directly.",
                }
            )
        elif any(phrase in lowered for phrase in ("list windows", "what windows are open", "show open windows")):
            payload["reply"] = "I can inspect the current desktop windows."
            payload["tool_calls"].append(
                {
                    "tool_id": "windows.list_windows",
                    "payload": {"limit": 40},
                    "reason": "Read the live desktop window list.",
                }
            )
        elif any(phrase in lowered for phrase in ("open notepad", "launch notepad")):
            payload["reply"] = "I can launch Notepad on Windows."
            payload["tool_calls"].append(
                {
                    "tool_id": "windows.launch_application",
                    "payload": {"command": "notepad.exe"},
                    "reason": "Open the requested Windows application directly.",
                }
            )
        elif any(phrase in lowered for phrase in ("open vscode", "open visual studio code", "launch vscode", "launch visual studio code")):
            payload["reply"] = "I can launch Visual Studio Code if it is installed."
            payload["tool_calls"].append(
                {
                    "tool_id": "windows.launch_application",
                    "payload": {"command": "code"},
                    "reason": "Open the requested Windows application directly.",
                }
            )
        elif self._wants_complex_browser_goal(lowered):
            payload["reply"] = "I can work through that browser task and open the best result for you."
            payload["tool_calls"].append(
                {
                    "tool_id": "browser.complete_goal",
                    "payload": {"instruction": user_message},
                    "reason": "Interpret a multi-step browser goal instead of treating the whole request as a literal search string.",
                }
            )
        elif any(phrase in lowered for phrase in ("open the browser", "open browser", "search google", "google search")):
            query = self._extract_search_query(user_message)
            payload["reply"] = "I can open a visible browser tab for that search."
            payload["tool_calls"].append(
                {
                    "tool_id": "windows.open_url",
                    "payload": {"url": self._google_search_url(query)},
                    "reason": "Open the requested Google search in the default browser.",
                }
            )
        elif "status" in lowered or "what's pending" in lowered or "what is pending" in lowered:
            payload["reply"] = (
                "Project Q status: "
                f"{len(context.get('tasks', []))} tracked tasks, "
                f"{len(context.get('agents', []))} agents, and "
                f"{len(context.get('routines', []))} routines, and "
                f"{len(context.get('memories', []))} recent memories."
            )

        return ReasonerPlan.model_validate(payload)

    def _developer_prompt(self) -> str:
        return (
            "You are Project Q, a private single-owner Windows executive agent. "
            "Be concise, action-oriented, and grounded in the provided context. "
            "Return JSON only with these keys: reply, memory_writes, task_writes, agent_writes, routine_writes, tool_calls. "
            "Use tool_calls only when a listed tool would materially improve correctness. "
            "Use routine_writes when the owner is asking for a reusable automation or trusted workflow. "
            "If the owner asks to create or modify a specific file now, do not use routine_writes unless they explicitly ask for an automation. "
            "For file creation or code generation inside the workspace, prefer filesystem.write_file over shell.run_command. "
            "For finding owner files, prefer filesystem.resolve_file_request so ambiguous results ask for confirmation and clear matches can open or reveal. "
            "Use filesystem.search_files only for raw search lists where no opening or selection is needed. "
            "For spreadsheet calculations, summaries, totals, averages, and workbook modifications, use spreadsheet.inspect, spreadsheet.analyze, or spreadsheet.write_analysis. "
            "For requests to troubleshoot Project Q itself, run self checks, debug recent mistakes, or inspect reliability, prefer diagnostics.run_self_check. "
            "For requests asking how Project Q can become ChatGPT, Codex, Claude, or AGI-like, prefer training.capability_plan. "
            "For requests to train model weights, fine-tune yourself, or create a LoRA job, prefer training.prepare_lora_job. "
            "For requests to export training data only, prefer training.export_dataset. "
            "For essays, writing, math, physics, history, quant, science, homework, and general problem solving, prefer knowledge.answer. "
            "For website, landing page, web app, and static site creation requests, prefer code.generate_website. "
            "For broader app, API, script, tool, or build requests that should create files, prefer code.generate_project. "
            "For refactor, research, or build requests that need tracking before implementation, prefer project.plan_build. "
            "For Python files, prefer pathlib, avoid hardcoded absolute Windows paths, and avoid backslash-joined f-string paths. "
            "For visible browser opening or simple web searches, prefer windows.open_url. "
            "For fuzzy, multi-step browser goals like finding tutorials, switching to images, or opening the best result, prefer browser.complete_goal. "
            "Use browser.run_actions only for exact in-browser steps and browser.inspect_page for read-only inspection. "
            "Prefer read-only tools before speculative answers. "
            "Never request secrets, never invent files, never exceed the listed tool ids, "
            "and avoid destructive actions unless clearly necessary."
        )

    def _user_prompt(self, *, user_message: str, context: dict[str, Any]) -> str:
        return (
            "Owner request:\n"
            f"{user_message}\n\n"
            "Available context JSON:\n"
            f"{json.dumps(context, ensure_ascii=True)}\n\n"
            "Produce a JSON object with:\n"
            "- reply: string\n"
            "- memory_writes: array of {text, kind, confidence}\n"
            "- task_writes: array of {title, description, priority} where priority is an integer from 1 to 5\n"
            "- agent_writes: array of {name, agent_type, goal, status, tools}\n"
            "- routine_writes: array of {name, goal, description, status, trigger_type, trusted, tools, steps, notes}\n"
            "  where steps are {step_type, label, tool_id, payload, agent_id, delay_seconds, continue_on_error, requires_owner_approval}\n"
            "  step_type must be exactly one of: tool, agent, delay. Never invent custom step types.\n"
            "- tool_calls: array of {tool_id, reason, payload} where payload must always be a JSON object, never a string\n"
            "  example shell payload: {\"command\": \"Get-ChildItem\", \"workdir\": \".\", \"timeout_seconds\": 15}\n"
            "  example write payload: {\"path\": \"notes/example.py\", \"content\": \"print('hi')\"}\n"
            "  example file search payload: {\"root\": \".\", \"query\": \"invoice\", \"include_content\": true, \"max_results\": 50}\n"
            "  example file resolve payload: {\"root\": \".\", \"instruction\": \"find resume for Muhammad Umar Qasim\", \"query\": \"resume\", \"action\": \"reveal\"}\n"
            "  example file choice payload: {\"choice_id\": \"choice_abc123\", \"selection\": 1, \"mode\": \"open\"}\n"
            "  example spreadsheet analyze payload: {\"path\": \"data/sales.xlsx\", \"operation\": \"sum\", \"column\": \"Sales Amount\"}\n"
            "  example spreadsheet write payload: {\"path\": \"data/sales.xlsx\", \"operation\": \"average\", \"column\": \"Sales Amount\"}\n"
            "  example diagnostics payload: {\"source\": \"chat\"}\n"
            "  example capability plan payload: {}\n"
            "  example training export payload: {\"reason\": \"chat\", \"max_records\": 200}\n"
            "  example LoRA training job payload: {\"reason\": \"chat\", \"base_model\": \"Qwen/Qwen2.5-Coder-1.5B-Instruct\", \"max_records\": 300}\n"
            "  example knowledge payload: {\"instruction\": \"solve 2x + 3 = 11\", \"depth\": \"standard\"}\n"
            "  example website payload: {\"instruction\": \"create a website for an IT consulting company\"}\n"
            "  example project generation payload: {\"instruction\": \"build a Python script that scans for TODO comments\"}\n"
            "  example project build payload: {\"instruction\": \"build a CRM dashboard app with login and reports\"}\n"
            "  example browser goal payload: {\"instruction\": \"find me a tutorial on how to setup this stand\"}\n"
            "  for Python content, use pathlib-based paths or forward slashes instead of raw Windows backslashes\n"
            "  if the owner asks to create a file right now, prefer tool_calls with filesystem.write_file and leave routine_writes empty unless they explicitly asked for automation\n"
            "Leave arrays empty when nothing should be created."
        )

    def _extract_text(self, response_payload: dict[str, Any]) -> str:
        if isinstance(response_payload.get("output_text"), str):
            return response_payload["output_text"]

        for item in response_payload.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text", "")
        raise ValueError("No model text found in provider response")

    def _extract_ollama_text(self, response_payload: dict[str, Any]) -> str:
        message = response_payload.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(response_payload.get("response"), str):
            return response_payload["response"]
        raise ValueError("No model text found in Ollama response")

    def _extract_json(self, text: str) -> dict[str, Any]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise
            return json.loads(text[start : end + 1])

    def _default_transport(self, url: str, body: bytes, headers: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Provider request failed: {exc.code} {details}") from exc

    def _default_get_transport(self, url: str, headers: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
        req = request.Request(url, headers=headers, method="GET")
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Provider request failed: {exc.code} {details}") from exc

    def _ollama_tags_url(self, base_url: str) -> str:
        parsed = urlsplit(base_url)
        path = parsed.path or "/api/chat"
        if "/api/" in path:
            prefix = path.split("/api/", 1)[0]
            path = prefix + "/api/tags"
        else:
            path = "/api/tags"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    def _agent_name(self, message: str) -> str:
        tokens = re.findall(r"[A-Za-z0-9]+", message)
        return " ".join(tokens[:4]).strip() or "Project Q Worker"

    def _agent_type(self, lowered_message: str) -> str:
        if "research" in lowered_message:
            return "research"
        if "test" in lowered_message:
            return "testing"
        if "website" in lowered_message or "code" in lowered_message or "build" in lowered_message:
            return "coding"
        return "general"

    def _heuristic_routine(self, goal: str, lowered_message: str, urls: list[str]) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        tools: list[str] = []

        if "list files" in lowered_message or "workspace" in lowered_message:
            steps.append(
                {
                    "step_type": "tool",
                    "label": "Inspect workspace",
                    "tool_id": "filesystem.list_directory",
                    "payload": {"path": "."},
                    "continue_on_error": False,
                    "requires_owner_approval": False,
                }
            )
            tools.append("filesystem.list_directory")

        if "notepad" in lowered_message:
            steps.append(
                {
                    "step_type": "tool",
                    "label": "Launch Notepad",
                    "tool_id": "windows.launch_application",
                    "payload": {"command": "notepad.exe"},
                    "continue_on_error": False,
                    "requires_owner_approval": True,
                }
            )
            tools.append("windows.launch_application")
        elif any(phrase in lowered_message for phrase in ("vscode", "visual studio code", "vs code")):
            steps.append(
                {
                    "step_type": "tool",
                    "label": "Launch VS Code",
                    "tool_id": "windows.launch_application",
                    "payload": {"command": "code"},
                    "continue_on_error": False,
                    "requires_owner_approval": True,
                }
            )
            tools.append("windows.launch_application")

        if urls:
            steps.append(
                {
                    "step_type": "tool",
                    "label": "Inspect reference page",
                    "tool_id": "browser.inspect_page",
                    "payload": {"url": urls[0], "include_links": True},
                    "continue_on_error": False,
                    "requires_owner_approval": False,
                }
            )
            tools.append("browser.inspect_page")

        if not steps:
            steps.append(
                {
                    "step_type": "tool",
                    "label": "Inspect workspace",
                    "tool_id": "filesystem.list_directory",
                    "payload": {"path": "."},
                    "continue_on_error": False,
                    "requires_owner_approval": False,
                }
            )
            tools.append("filesystem.list_directory")

        return {
            "name": self._agent_name(goal),
            "goal": goal,
            "description": "Heuristic routine created from the owner request.",
            "status": "active",
            "trigger_type": "manual",
            "trusted": False,
            "tools": tools,
            "steps": steps,
            "notes": "",
        }

    def _extract_search_query(self, message: str) -> str:
        match = re.search(r"search(?:\s+google)?(?:\s+for)?\s+(.+)$", message, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" .")
        return message.strip()

    def _wants_spreadsheet_work(self, lowered: str) -> bool:
        spreadsheet_markers = (".xlsx", ".csv", ".tsv", "excel", "spreadsheet", "workbook", "sheet")
        analysis_markers = (
            "sum",
            "total",
            "average",
            "avg",
            "mean",
            "count",
            "minimum",
            "maximum",
            "min",
            "max",
            "analyze",
            "analyse",
            "summary",
            "modify",
            "update",
        )
        return any(marker in lowered for marker in spreadsheet_markers) and any(
            marker in lowered for marker in analysis_markers
        )

    def _extract_spreadsheet_path(self, message: str) -> str:
        windows_match = re.search(r"([A-Za-z]:\\[^\r\n]+?\.(?:xlsx|csv|tsv))", message, flags=re.IGNORECASE)
        if windows_match:
            return windows_match.group(1).strip(" .\"'")
        relative_match = re.search(r"([^\s\"']+\.(?:xlsx|csv|tsv))", message, flags=re.IGNORECASE)
        if relative_match:
            return relative_match.group(1).strip(" .\"'")
        return ""

    def _extract_spreadsheet_operation(self, lowered: str) -> str:
        if any(word in lowered for word in ("average", "avg", "mean")):
            return "average"
        if any(word in lowered for word in ("total", "sum", "add up")):
            return "sum"
        if "count" in lowered:
            return "count"
        if any(word in lowered for word in ("minimum", "lowest", "min")):
            return "min"
        if any(word in lowered for word in ("maximum", "highest", "max")):
            return "max"
        return "profile"

    def _extract_spreadsheet_column(self, message: str, operation: str, spreadsheet_path: str) -> str:
        working = message
        if spreadsheet_path:
            working = working.replace(spreadsheet_path, "")
        working = re.sub(r"\s+by\s+.+?(?:\s+in\s*$|\s+from\s*$|$)", "", working, flags=re.IGNORECASE)
        operation_words = {
            "sum": r"total|sum|add up",
            "average": r"average|avg|mean",
            "count": r"count",
            "min": r"minimum|lowest|min",
            "max": r"maximum|highest|max",
            "profile": r"analyze|analyse|summary|profile",
        }.get(operation, operation)
        match = re.search(
            rf"(?:{operation_words})\s+(?:of\s+|for\s+|the\s+)?(.+?)(?:\s+in\s*$|\s+from\s*$|$)",
            working,
            flags=re.IGNORECASE,
        )
        if match:
            column = match.group(1).strip(" .")
            column = re.sub(r"^(?:the\s+)?(?:column\s+)?", "", column, flags=re.IGNORECASE).strip()
            if column and column.lower() not in {"spreadsheet", "workbook", "sheet"}:
                return column
        return ""

    def _extract_spreadsheet_group_by(self, message: str, spreadsheet_path: str) -> str:
        working = message
        if spreadsheet_path:
            working = working.replace(spreadsheet_path, "")
        match = re.search(r"\bby\s+(.+?)(?:\s+in\s*$|\s+from\s*$|$)", working, flags=re.IGNORECASE)
        if not match:
            return ""
        group_by = match.group(1).strip(" .")
        return re.sub(r"^(?:the\s+)?(?:column\s+)?", "", group_by, flags=re.IGNORECASE).strip()

    def _wants_spreadsheet_write(self, lowered: str) -> bool:
        return any(word in lowered for word in ("write", "add", "create", "modify", "update", "save")) and any(
            word in lowered for word in ("analysis", "summary", "sheet", "tab", "workbook")
        )

    def _wants_file_search(self, lowered: str) -> bool:
        return any(
            phrase in lowered
            for phrase in (
                "search my files",
                "search files",
                "find my file",
                "find file",
                "find files",
                "look through my files",
                "scan my files",
                "open file",
                "open the file",
                "reveal file",
                "show file",
            )
        )

    def _extract_file_search_query(self, message: str) -> str:
        patterns = (
            r"(?:search|scan|look through)\s+(?:my\s+)?files\s+(?:for|about|matching)?\s*(.+)$",
            r"find\s+(?:my\s+)?files?\s+(?:for|about|called|named|matching)?\s*(.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, message, flags=re.IGNORECASE)
            if match and match.group(1).strip():
                return match.group(1).strip(" .")
        return message.strip()

    def _default_file_search_root(self, context: dict[str, Any]) -> str:
        roots = ((context.get("owner") or {}).get("file_access_roots") or [])
        if roots:
            return str(roots[0])
        return "."

    def _file_action_for_request(self, lowered: str) -> str:
        if any(phrase in lowered for phrase in ("open file", "open the file", "open my file")):
            return "open"
        return "reveal"

    def _wants_file_choice_followup(self, lowered: str) -> bool:
        return re.search(r"\b(open|reveal|select|show)\s+(?:file\s+)?(?:option|choice|#)?\s*\d+\b", lowered) is not None

    def _extract_file_choice_selection(self, lowered: str) -> int:
        match = re.search(r"\b(?:option|choice|#)?\s*(\d+)\b", lowered)
        if not match:
            return 1
        return max(1, int(match.group(1)))

    def _extract_recent_file_choice_id(self, context: dict[str, Any]) -> str:
        for message in context.get("recent_messages", []):
            if message.get("role") != "assistant":
                continue
            match = re.search(r"choice_[a-f0-9]{12}", message.get("content", ""))
            if match:
                return match.group(0)
        return ""

    def _wants_self_diagnostics(self, lowered: str) -> bool:
        return any(
            phrase in lowered
            for phrase in (
                "run diagnostics",
                "self diagnostics",
                "diagnose yourself",
                "debug yourself",
                "troubleshoot yourself",
                "check yourself",
                "fix your mistakes",
                "own mistakes",
                "test yourself",
            )
        )

    def _wants_capability_plan(self, lowered: str) -> bool:
        capability_targets = ("chatgpt", "codex", "claude", "agi", "frontier model", "frontier-model")
        ambition_markers = (
            "how can",
            "how do",
            "make it",
            "become",
            "level",
            "extremely intelligent",
            "more intelligent",
            "like you",
        )
        return any(target in lowered for target in capability_targets) and any(
            marker in lowered for marker in ambition_markers
        )

    def _wants_training_request(self, lowered: str) -> bool:
        return self._wants_lora_training_job(lowered) or self._wants_training_export(lowered)

    def _wants_lora_training_job(self, lowered: str) -> bool:
        return any(
            phrase in lowered
            for phrase in (
                "train model weights",
                "fine tune",
                "fine-tune",
                "train yourself",
                "train your self",
                "update model weights",
                "local lora job",
                "lora job",
                "weight training",
            )
        )

    def _wants_training_export(self, lowered: str) -> bool:
        return any(
            phrase in lowered
            for phrase in (
                "training dataset",
                "export training",
                "training assets",
                "routing eval",
                "sft dataset",
                "preference dataset",
            )
        )

    def _wants_knowledge_work(self, lowered: str) -> bool:
        if lowered.startswith(("remember ", "task:", "todo:", "automation:", "routine:")):
            return False
        knowledge_markers = (
            "write an essay",
            "write a five paragraph essay",
            "write a 5 paragraph essay",
            "essay",
            "draft a paper",
            "compose",
            "solve this",
            "solve ",
            "math",
            "physics",
            "history",
            "historical",
            "quant",
            "calculate",
            "derive",
            "explain",
            "homework",
            "problem",
            "statistics",
            "calculus",
            "algebra",
            "chemistry",
            "biology",
            "economics",
        )
        return any(marker in lowered for marker in knowledge_markers)

    def _wants_website_generation(self, lowered: str) -> bool:
        if lowered.startswith(("remember ", "task:", "todo:", "automation:", "routine:")):
            return False
        creation_markers = (
            "create",
            "build",
            "make",
            "generate",
            "scaffold",
            "code",
            "design",
        )
        site_markers = (
            "website",
            "web site",
            "landing page",
            "webpage",
            "web page",
            "static site",
            "web app",
            "homepage",
        )
        return any(marker in lowered for marker in creation_markers) and any(marker in lowered for marker in site_markers)

    def _wants_project_build(self, lowered: str) -> bool:
        if lowered.startswith(("remember ", "task:", "todo:", "automation:", "routine:")):
            return False
        creation_markers = (
            "build",
            "create",
            "make",
            "generate",
            "scaffold",
            "code",
            "write",
            "implement",
            "design",
            "fix",
            "debug",
            "refactor",
            "research",
        )
        build_markers = (
            "app",
            "application",
            "dashboard",
            "portal",
            "crm",
            "saas",
            "api",
            "backend",
            "script",
            "tool",
            "automation",
            "feature",
            "bug",
            "repo",
            "project",
            "software",
            "program",
        )
        return any(marker in lowered for marker in creation_markers) and any(
            marker in lowered for marker in build_markers
        )

    def _should_override_noop_plan(self, *, plan: ReasonerPlan, user_message: str) -> bool:
        if plan.memory_writes or plan.task_writes or plan.agent_writes or plan.routine_writes or plan.tool_calls:
            return False
        if not self._looks_actionable_request(user_message.lower()):
            return False
        reply = plan.reply.strip().lower()
        no_op_markers = (
            "will not create",
            "won't create",
            "not create any",
            "no new memory",
            "nothing should be created",
            "leave arrays empty",
            "not take any action",
            "no action",
        )
        return not reply or any(marker in reply for marker in no_op_markers)

    def _looks_actionable_request(self, lowered: str) -> bool:
        if lowered.startswith(("remember ", "task:", "todo:")):
            return False
        action_markers = (
            "analyze",
            "automate",
            "build",
            "calculate",
            "code",
            "create",
            "debug",
            "design",
            "draft",
            "find",
            "fix",
            "generate",
            "implement",
            "make",
            "open",
            "organize",
            "plan",
            "prepare",
            "refactor",
            "research",
            "search",
            "solve",
            "summarize",
            "write",
        )
        return any(marker in lowered for marker in action_markers)

    def _actionable_fallback_plan(self, *, user_message: str, context: dict[str, Any]) -> ReasonerPlan:
        local_plan = self._plan_local(user_message=user_message, context=context)
        if (
            local_plan.memory_writes
            or local_plan.task_writes
            or local_plan.agent_writes
            or local_plan.routine_writes
            or local_plan.tool_calls
        ):
            return local_plan

        lowered = user_message.lower()
        payload: dict[str, Any] = {
            "reply": "I will not leave that as a no-op. I converted it into the closest safe local action.",
            "memory_writes": [],
            "task_writes": [],
            "agent_writes": [],
            "routine_writes": [],
            "tool_calls": [],
        }
        if any(marker in lowered for marker in ("checklist", "draft", "prepare", "outline", "write", "explain")):
            payload["reply"] = "I can draft that directly with the local knowledge workbench."
            payload["tool_calls"].append(
                {
                    "tool_id": "knowledge.answer",
                    "payload": {"instruction": user_message, "depth": "local-only"},
                    "reason": "Fallback from a no-op provider response to a useful local answer.",
                }
            )
        elif self._wants_project_build(lowered):
            payload["reply"] = "I can generate a real Project Q project scaffold instead of accepting a no-op provider response."
            payload["tool_calls"].append(
                {
                    "tool_id": "code.generate_project",
                    "payload": {"instruction": user_message},
                    "reason": "Fallback from a no-op provider response to real local project generation.",
                }
            )
        else:
            title = re.sub(r"\s+", " ", user_message).strip()[:120] or "Handle owner request"
            payload["reply"] = "I converted that actionable request into a tracked task instead of doing nothing."
            payload["task_writes"].append(
                {
                    "title": f"Handle: {title}"[:120],
                    "description": user_message,
                    "priority": 3,
                }
            )
        return ReasonerPlan.model_validate(payload)

    def _google_search_url(self, query: str) -> str:
        safe_query = query.strip() or "Google"
        return "https://www.google.com/search?q=" + quote(safe_query)

    def _normalize_plan_for_request(self, *, plan: ReasonerPlan, user_message: str) -> ReasonerPlan:
        lowered = user_message.lower()
        if self._wants_complex_browser_goal(lowered):
            normalized = plan.model_dump()
            normalized["memory_writes"] = []
            normalized["task_writes"] = []
            normalized["agent_writes"] = []
            normalized["routine_writes"] = []
            normalized["tool_calls"] = [
                {
                    "tool_id": "browser.complete_goal",
                    "reason": "Interpret the owner's multi-step browser goal and carry it through intelligently.",
                    "payload": {"instruction": user_message},
                }
            ]
            normalized["reply"] = "I can work through that browser task and open the best result for you."
            return ReasonerPlan.model_validate(normalized)
        if self._wants_visible_browser_action(lowered):
            normalized = plan.model_dump()
            normalized["memory_writes"] = []
            normalized["task_writes"] = []
            normalized["agent_writes"] = []
            normalized["routine_writes"] = []
            normalized["tool_calls"] = [
                {
                    "tool_id": "windows.open_url",
                    "reason": "Open the requested Google search in the default browser.",
                    "payload": {"url": self._google_search_url(self._extract_search_query(user_message))},
                }
            ]
            normalized["reply"] = "I can open a visible browser tab for that search."
            return ReasonerPlan.model_validate(normalized)
        return plan

    def _status_model_name(self, settings: dict[str, Any]) -> str | None:
        provider_type = settings.get("provider_type", "ollama")
        if provider_type != "ollama":
            return settings.get("model_name") or None
        if settings.get("ollama_model_routing_enabled", True):
            return settings.get("ollama_general_model") or settings.get("model_name") or None
        return settings.get("model_name") or settings.get("ollama_general_model") or None

    def _provider_timeout_seconds(self, settings: dict[str, Any]) -> int:
        timeout_seconds = int(settings.get("provider_timeout_seconds", 120))
        if settings.get("provider_type", "ollama") == "ollama":
            return max(timeout_seconds, 120)
        return timeout_seconds

    def _provider_model_for_request(
        self,
        *,
        user_message: str,
        context: dict[str, Any],
        settings: dict[str, Any],
    ) -> str | None:
        provider_type = settings.get("provider_type", "ollama")
        if provider_type != "ollama":
            return settings.get("model_name") or None
        return self._select_ollama_model(user_message=user_message, context=context, settings=settings)

    def _select_ollama_model(
        self,
        *,
        user_message: str,
        context: dict[str, Any],
        settings: dict[str, Any],
    ) -> str | None:
        if not settings.get("ollama_model_routing_enabled", True):
            return settings.get("model_name") or settings.get("ollama_general_model") or None

        lowered = user_message.lower()
        if self._looks_like_coding_request(lowered):
            return (
                settings.get("ollama_coding_model")
                or settings.get("ollama_general_model")
                or settings.get("model_name")
                or None
            )
        if self._looks_like_reasoning_request(lowered):
            return (
                settings.get("ollama_reasoning_model")
                or settings.get("ollama_general_model")
                or settings.get("model_name")
                or None
            )
        if self._looks_like_fast_request(lowered, context):
            return (
                settings.get("ollama_fast_model")
                or settings.get("ollama_general_model")
                or settings.get("model_name")
                or None
            )
        return settings.get("ollama_general_model") or settings.get("model_name") or None

    def _looks_like_coding_request(self, lowered: str) -> bool:
        coding_markers = (
            "code",
            "coding",
            "debug",
            "fix bug",
            "fix this",
            "refactor",
            "website",
            "web app",
            "react",
            "typescript",
            "javascript",
            "python",
            "html",
            "css",
            "sql",
            "api",
            "script",
            "repo",
            "repository",
            "git",
            "function",
            "class ",
            "pull request",
        )
        file_markers = (".py", ".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".json", ".sql", ".md")
        return any(marker in lowered for marker in coding_markers) or any(marker in lowered for marker in file_markers)

    def _looks_like_reasoning_request(self, lowered: str) -> bool:
        reasoning_markers = (
            "plan",
            "strategy",
            "analyze",
            "analysis",
            "compare",
            "comparison",
            "tradeoff",
            "trade-off",
            "why should",
            "should i",
            "pros and cons",
            "risk",
            "decide",
            "decision",
            "brainstorm",
            "outline approach",
        )
        return any(marker in lowered for marker in reasoning_markers)

    def _looks_like_fast_request(self, lowered: str, context: dict[str, Any]) -> bool:
        del context
        fast_markers = (
            "hello",
            "hi",
            "status",
            "what's pending",
            "what is pending",
            "list files",
            "show files",
            "list windows",
            "what windows are open",
            "open notepad",
            "open vscode",
            "launch vscode",
            "launch notepad",
            "summarize this page",
            "quick",
        )
        return len(lowered) < 90 and any(marker in lowered for marker in fast_markers)

    def _wants_visible_browser_action(self, lowered: str) -> bool:
        return any(
            phrase in lowered
            for phrase in ("open the browser", "open browser", "search google", "google search")
        )

    def _wants_complex_browser_goal(self, lowered: str) -> bool:
        return (
            self._wants_visible_browser_action(lowered)
            and any(
                phrase in lowered
                for phrase in (
                    " then ",
                    " than ",
                    "go to images",
                    "go to videos",
                    "select videos",
                    "youtube link",
                    "find me a tutorial",
                    "tutorial",
                    "guide",
                    "high quality image",
                    "play the video",
                )
            )
        ) or lowered.startswith("find me a tutorial") or lowered.startswith("find me a guide")
