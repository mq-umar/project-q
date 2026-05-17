from __future__ import annotations

import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from project_q.app import ProjectQApplication, create_application
from project_q.config import AppConfig
from project_q.models import (
    AgentCreate,
    AgentRunRequest,
    AgentUpdate,
    ChatRequest,
    LearningRunOnceRequest,
    LearningStartRequest,
    MemoryCreate,
    MemoryUpdate,
    RoutineCreate,
    RoutineRunRequest,
    RoutineUpdate,
    SecretUpsert,
    SettingsUpdate,
    TaskCreate,
    TaskUpdate,
    ToolExecutionRequest,
)


STATIC_ROOT = Path(__file__).resolve().parent / "static"


class ProjectQHandler(BaseHTTPRequestHandler):
    app: ProjectQApplication

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/"):
            self._route_api("GET")
            return
        self._serve_static()

    def do_POST(self) -> None:  # noqa: N802
        self._route_api("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._route_api("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route_api("DELETE")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _route_api(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        body = self._json_body() if method in {"POST", "PUT"} else {}

        routes = [
            ("GET", r"^/api/status$", self._get_status),
            ("GET", r"^/api/conversations$", self._get_conversations),
            ("POST", r"^/api/chat$", self._post_chat),
            ("GET", r"^/api/memories$", self._list_memories),
            ("POST", r"^/api/memories$", self._create_memory),
            ("PUT", r"^/api/memories/([^/]+)$", self._update_memory),
            ("DELETE", r"^/api/memories/([^/]+)$", self._delete_memory),
            ("GET", r"^/api/tasks$", self._list_tasks),
            ("POST", r"^/api/tasks$", self._create_task),
            ("PUT", r"^/api/tasks/([^/]+)$", self._update_task),
            ("DELETE", r"^/api/tasks/([^/]+)$", self._delete_task),
            ("GET", r"^/api/agents$", self._list_agents),
            ("POST", r"^/api/agents$", self._create_agent),
            ("GET", r"^/api/agents/([^/]+)/runs$", self._list_agent_runs),
            ("POST", r"^/api/agents/([^/]+)/run$", self._run_agent),
            ("PUT", r"^/api/agents/([^/]+)$", self._update_agent),
            ("DELETE", r"^/api/agents/([^/]+)$", self._delete_agent),
            ("GET", r"^/api/routines$", self._list_routines),
            ("POST", r"^/api/routines$", self._create_routine),
            ("GET", r"^/api/routines/([^/]+)/runs$", self._list_routine_runs),
            ("POST", r"^/api/routines/([^/]+)/run$", self._run_routine),
            ("PUT", r"^/api/routines/([^/]+)$", self._update_routine),
            ("DELETE", r"^/api/routines/([^/]+)$", self._delete_routine),
            ("GET", r"^/api/audit$", self._list_audit),
            ("GET", r"^/api/settings$", self._get_settings),
            ("PUT", r"^/api/settings$", self._update_settings),
            ("GET", r"^/api/provider/models$", self._list_provider_models),
            ("GET", r"^/api/learning/status$", self._learning_status),
            ("GET", r"^/api/learning/runs$", self._learning_runs),
            ("POST", r"^/api/learning/start$", self._learning_start),
            ("POST", r"^/api/learning/stop$", self._learning_stop),
            ("POST", r"^/api/learning/run-once$", self._learning_run_once),
            ("GET", r"^/api/diagnostics/runs$", self._diagnostic_runs),
            ("POST", r"^/api/diagnostics/run$", self._diagnostic_run),
            ("POST", r"^/api/diagnostics/auto-repair$", self._diagnostic_auto_repair),
            ("GET", r"^/api/dispatches$", self._list_dispatches),
            ("POST", r"^/api/training/export$", self._training_export),
            ("POST", r"^/api/training/prepare-lora$", self._training_prepare_lora),
            ("GET", r"^/api/tools$", self._list_tools),
            ("POST", r"^/api/tools/execute$", self._execute_tool),
            ("GET", r"^/api/secrets$", self._list_secrets),
            ("POST", r"^/api/secrets$", self._upsert_secret),
            ("DELETE", r"^/api/secrets/([^/]+)$", self._delete_secret),
        ]

        try:
            for route_method, pattern, handler in routes:
                match = re.match(pattern, path)
                if route_method == method and match:
                    return handler(match.groups(), body, query)
            self._json_response({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
        except KeyError as exc:
            self._json_response({"error": f"Not found: {exc}"}, status=HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self._json_response({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except Exception as exc:  # noqa: BLE001
            self.app.audit.log(
                action_type="server_error",
                action_tier=0,
                tool_name="http_server",
                outcome="failed",
                error=str(exc),
            )
            self._json_response(
                {"error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _get_status(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response(
            {
                "project": self.app.config.project_name,
                "workspace_root": str(self.app.config.workspace_root),
                "data_root": str(self.app.config.data_root),
                "tool_count": len(self.app.tools.describe_all()),
                "status": "online",
                "reasoner": self.app.reasoner.status(),
                "runtime": self.app.runtime_metadata(),
            }
        )

    def _get_conversations(self, _args: tuple[str, ...], _body: dict[str, Any], query: dict[str, Any]) -> None:
        limit = int(query.get("limit", ["100"])[0])
        self._json_response({"items": self.app.conversations.list_messages(limit=limit)})

    def _post_chat(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        response = self.app.conversations.respond(ChatRequest.model_validate(body))
        self.app.audit.log(
            action_type="chat_turn",
            action_tier=0,
            tool_name="conversation",
            outcome="completed",
            metadata=response.model_dump(),
            input_sources=["owner"],
        )
        self._json_response(response.model_dump())

    def _list_memories(self, _args: tuple[str, ...], _body: dict[str, Any], query: dict[str, Any]) -> None:
        search = query.get("q", [""])[0]
        items = self.app.memory.search(search) if search else self.app.memory.list_all()
        self._json_response({"items": items})

    def _create_memory(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        memory = self.app.memory.create(MemoryCreate.model_validate(body))
        self.app.audit.log(
            action_type="memory_create",
            action_tier=1,
            tool_name="memory_service",
            outcome="completed",
            approved_by_owner=True,
            metadata={"memory_id": memory["id"]},
        )
        self._json_response(memory, status=HTTPStatus.CREATED)

    def _update_memory(self, args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        memory = self.app.memory.update(args[0], MemoryUpdate.model_validate(body))
        self._json_response(memory)

    def _delete_memory(self, args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self.app.memory.delete(args[0])
        self._json_response({"deleted": args[0]})

    def _list_tasks(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response({"items": self.app.tasks.list_all()})

    def _create_task(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        task = self.app.tasks.create(TaskCreate.model_validate(body))
        self.app.audit.log(
            action_type="task_create",
            action_tier=1,
            tool_name="task_service",
            outcome="completed",
            approved_by_owner=True,
            metadata={"task_id": task["id"]},
        )
        self._json_response(task, status=HTTPStatus.CREATED)

    def _update_task(self, args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response(self.app.tasks.update(args[0], TaskUpdate.model_validate(body)))

    def _delete_task(self, args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self.app.tasks.delete(args[0])
        self._json_response({"deleted": args[0]})

    def _list_agents(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response({"items": self.app.agents.list_all()})

    def _create_agent(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        agent = self.app.agents.create(AgentCreate.model_validate(body))
        self.app.audit.log(
            action_type="agent_create",
            action_tier=1,
            tool_name="agent_service",
            outcome="completed",
            approved_by_owner=True,
            metadata={"agent_id": agent["id"]},
        )
        self._json_response(agent, status=HTTPStatus.CREATED)

    def _update_agent(self, args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response(self.app.agents.update(args[0], AgentUpdate.model_validate(body)))

    def _list_agent_runs(self, args: tuple[str, ...], _body: dict[str, Any], query: dict[str, Any]) -> None:
        limit = int(query.get("limit", ["20"])[0])
        self._json_response({"items": self.app.agent_runner.list_runs(args[0], limit=limit)})

    def _run_agent(self, args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        payload = AgentRunRequest.model_validate(body)
        self._json_response(self.app.agent_runner.run(args[0], owner_approved=payload.owner_approved))

    def _delete_agent(self, args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self.app.agents.delete(args[0])
        self._json_response({"deleted": args[0]})

    def _list_routines(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response({"items": self.app.routines.list_all()})

    def _create_routine(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        routine = self.app.routines.create(RoutineCreate.model_validate(body))
        self.app.audit.log(
            action_type="routine_create",
            action_tier=1,
            tool_name="routine_service",
            outcome="completed",
            approved_by_owner=True,
            metadata={"routine_id": routine["id"]},
        )
        self._json_response(routine, status=HTTPStatus.CREATED)

    def _update_routine(self, args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response(self.app.routines.update(args[0], RoutineUpdate.model_validate(body)))

    def _list_routine_runs(self, args: tuple[str, ...], _body: dict[str, Any], query: dict[str, Any]) -> None:
        limit = int(query.get("limit", ["20"])[0])
        self._json_response({"items": self.app.routine_runner.list_runs(args[0], limit=limit)})

    def _run_routine(self, args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        payload = RoutineRunRequest.model_validate(body)
        self._json_response(self.app.routine_runner.run(args[0], owner_approved=payload.owner_approved))

    def _delete_routine(self, args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self.app.routines.delete(args[0])
        self._json_response({"deleted": args[0]})

    def _list_audit(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response({"items": self.app.audit.list_recent()})

    def _get_settings(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response(self.app.settings.get_all())

    def _update_settings(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response(self.app.settings.update(SettingsUpdate.model_validate(body)))

    def _list_provider_models(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response(self.app.reasoner.list_available_models())

    def _learning_status(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response(self.app.learning.status())

    def _learning_runs(self, _args: tuple[str, ...], _body: dict[str, Any], query: dict[str, Any]) -> None:
        limit = int(query.get("limit", ["20"])[0])
        self._json_response({"items": self.app.learning.list_runs(limit=limit)})

    def _learning_start(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        payload = LearningStartRequest.model_validate(body)
        self._json_response(self.app.learning.start(payload.max_cycles, payload.interval_seconds))

    def _learning_stop(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response(self.app.learning.stop())

    def _learning_run_once(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        payload = LearningRunOnceRequest.model_validate(body)
        self._json_response(self.app.learning.run_once(reason=payload.reason))

    def _diagnostic_runs(self, _args: tuple[str, ...], _body: dict[str, Any], query: dict[str, Any]) -> None:
        limit = int(query.get("limit", ["20"])[0])
        self._json_response({"items": self.app.diagnostics.list_runs(limit=limit)})

    def _diagnostic_run(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        source = str(body.get("source", "dashboard")).strip() or "dashboard"
        auto_repair = str(body.get("auto_repair", True)).strip().lower() not in {"0", "false", "no", "off"}
        self._json_response(self.app.diagnostics.run(source=source, auto_repair=auto_repair))

    def _diagnostic_auto_repair(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        source = str(body.get("source", "dashboard")).strip() or "dashboard"
        self._json_response(self.app.self_repair.run(source=source))

    def _list_dispatches(self, _args: tuple[str, ...], _body: dict[str, Any], query: dict[str, Any]) -> None:
        limit = int(query.get("limit", ["20"])[0])
        self._json_response({"items": self.app.dispatches.list_all(limit=limit)})

    def _training_export(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        reason = str(body.get("reason", "dashboard")).strip() or "dashboard"
        max_records = int(body.get("max_records", 200) or 200)
        self._json_response(self.app.training.export_dataset(reason=reason, max_records=max_records))

    def _training_prepare_lora(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        reason = str(body.get("reason", "dashboard")).strip() or "dashboard"
        base_model = str(body.get("base_model", "Qwen/Qwen2.5-Coder-1.5B-Instruct")).strip()
        max_records = int(body.get("max_records", 300) or 300)
        max_steps = int(body.get("max_steps", 120) or 120)
        max_seq_length = int(body.get("max_seq_length", 1536) or 1536)
        self._json_response(
            self.app.training.prepare_lora_job(
                reason=reason,
                base_model=base_model,
                max_records=max_records,
                max_steps=max_steps,
                max_seq_length=max_seq_length,
            )
        )

    def _list_tools(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response({"items": self.app.tools.describe_all()})

    def _execute_tool(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        request = ToolExecutionRequest.model_validate(body)
        tool = self.app.tools.get(request.tool_id)
        decision = self.app.policy.authorize_tool(
            tier=tool.definition.tier,
            owner_approved=request.owner_approved,
        )
        if not decision.allowed:
            self.app.audit.log(
                action_type="tool_execution",
                action_tier=tool.definition.tier,
                tool_name=request.tool_id,
                outcome="blocked",
                input_sources=["owner"],
                metadata={"reason": decision.reason, "payload": request.payload},
            )
            self._json_response({"error": decision.reason}, status=HTTPStatus.FORBIDDEN)
            return

        result = tool.execute(request.payload)
        self.app.audit.log(
            action_type="tool_execution",
            action_tier=tool.definition.tier,
            tool_name=request.tool_id,
            outcome="completed",
            approved_by_owner=request.owner_approved,
            input_sources=["owner"],
            metadata={"payload": request.payload, "result_preview": str(result)[:400]},
        )
        self._json_response({"result": result, "policy_reason": decision.reason})

    def _list_secrets(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response({"items": self.app.vault.list_secret_names()})

    def _upsert_secret(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        payload = SecretUpsert.model_validate(body)
        self.app.vault.set_secret(payload.name, payload.value, payload.description)
        self.app.audit.log(
            action_type="secret_upsert",
            action_tier=3,
            tool_name="vault_service",
            outcome="completed",
            approved_by_owner=True,
            metadata={"secret_name": payload.name},
        )
        self._json_response({"stored": payload.name}, status=HTTPStatus.CREATED)

    def _delete_secret(self, args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self.app.vault.delete_secret(args[0])
        self._json_response({"deleted": args[0]})

    def _json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length)
        return json.loads(raw.decode("utf-8"))

    def _json_response(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _serve_static(self) -> None:
        path = self.path
        if path == "/":
            requested = STATIC_ROOT / "index.html"
        else:
            requested = (STATIC_ROOT / path.lstrip("/")).resolve()
        if not requested.is_relative_to(STATIC_ROOT) or not requested.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type, _ = mimetypes.guess_type(str(requested))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        content = requested.read_bytes()
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def run_server(config: AppConfig | None = None) -> ThreadingHTTPServer:
    application = create_application(config)
    application.write_runtime_metadata()
    handler_cls = type("BoundProjectQHandler", (ProjectQHandler,), {"app": application})
    server = ThreadingHTTPServer((application.config.host, application.config.port), handler_cls)
    print(
        "Project Q running at "
        f"http://{application.config.host}:{application.config.port} "
        f"({application.build_id}, pid {application.process_id})"
    )
    return server


def main() -> None:
    server = run_server()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.RequestHandlerClass.app.clear_runtime_metadata()
        server.server_close()


if __name__ == "__main__":
    main()
