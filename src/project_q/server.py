from __future__ import annotations

import base64
import io
import ipaddress
import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import qrcode
except ModuleNotFoundError:  # Optional until project dependencies are installed.
    qrcode = None

from project_q.app import ProjectQApplication, create_application
from project_q.config import AppConfig
from project_q.models import (
    AgentCreate,
    AgentRunRequest,
    AgentUpdate,
    ChatRequest,
    CompanionPairingCompleteRequest,
    CompanionPairingStartRequest,
    CompanionQuickCaptureRequest,
    CompanionV2ApprovalDecisionRequest,
    CompanionV2ChatRequest,
    CompanionV2MemoryCreateRequest,
    CompanionV2MemoryUpdateRequest,
    CompanionV2PairingCompleteRequest,
    CompanionV2PresenceRequest,
    CompanionV2QuickCaptureRequest,
    CompanionV2SyncAckRequest,
    ControlRequest,
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
    VoiceTranscriptRequest,
)


STATIC_ROOT = Path(__file__).resolve().parent / "static"
OWNER_SESSION_COOKIE = "project_q_owner_session"
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024


def _host_name_from_header(host_header: str) -> str:
    host = str(host_header or "").strip().lower().rstrip(".")
    if not host:
        return ""
    if host.startswith("["):
        closing = host.find("]")
        return host[1:closing] if closing != -1 else host
    if host.count(":") > 1:
        return host
    return host.split(":", 1)[0]


def _is_allowed_host_header(host_header: str, configured_host: str) -> bool:
    host = _host_name_from_header(host_header)
    if not host:
        return True
    configured = _host_name_from_header(configured_host)
    allowed = {"localhost", "127.0.0.1", "::1"}
    if configured and configured not in {"0.0.0.0", "::"}:
        allowed.add(configured)
    if host in allowed:
        return True
    if configured not in {"0.0.0.0", "::"}:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        (address.is_private or address.is_loopback or address.is_link_local)
        and not address.is_unspecified
        and not address.is_multicast
    )


def _is_allowed_origin_header(origin_header: str, configured_host: str) -> bool:
    origin = str(origin_header or "").strip()
    if not origin:
        return True
    parsed = urlparse(origin)
    if not parsed.netloc:
        return False
    return _is_allowed_host_header(parsed.netloc, configured_host)


class ProjectQHandler(BaseHTTPRequestHandler):
    app: ProjectQApplication

    def do_GET(self) -> None:  # noqa: N802
        if not self._request_allowed("GET"):
            return
        if self.path.startswith("/api/"):
            self._route_api("GET")
            return
        self._serve_static()

    def do_POST(self) -> None:  # noqa: N802
        if not self._request_allowed("POST"):
            return
        # Streaming chat — handled outside _route_api to avoid JSON response wrapper
        if self.path == "/api/companion/v2/chat/stream":
            try:
                body = self._json_body()
            except ValueError as exc:
                self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                context = self._verify_companion_v2_auth()
                if context is None:
                    return
                self._companion_device_context = context
                if self._companion_v2_blocked_by_kill_switch("POST", self.path):
                    return
                self._stream_companion_v2_chat((), body, {})
            except PermissionError as exc:
                self._json_response({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except ValueError as exc:
                self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # noqa: BLE001
                self.app.audit.log(
                    action_type="companion_v2_stream_error",
                    action_tier=0,
                    tool_name="http_server",
                    outcome="failed",
                    error=str(exc),
                )
                self._json_response(
                    {"error": "companion stream failed"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if self.path == "/api/chat/stream":
            if not self._owner_session_allowed():
                return
            try:
                body = self._json_body()
            except ValueError as exc:
                self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                self._stream_chat((), body, {})
            except ValueError as exc:
                self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # noqa: BLE001
                self.app.audit.log(
                    action_type="stream_error",
                    action_tier=0,
                    tool_name="http_server",
                    outcome="failed",
                    error=str(exc),
                )
            return
        self._route_api("POST")

    def do_PUT(self) -> None:  # noqa: N802
        if not self._request_allowed("PUT"):
            return
        self._route_api("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._request_allowed("DELETE"):
            return
        self._route_api("DELETE")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _request_allowed(self, method: str) -> bool:
        if not _is_allowed_host_header(self.headers.get("Host", ""), self.app.config.host):
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden host")
            return False
        if method in {"POST", "PUT", "DELETE"} and not _is_allowed_origin_header(
            self.headers.get("Origin", ""),
            self.app.config.host,
        ):
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden origin")
            return False
        return True

    def _route_api(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path.startswith("/api/companion/v2/"):
            self._route_companion_v2(method, path, query)
            return
        if self._requires_owner_session(method, path) and not self._owner_session_allowed():
            return
        if self._blocked_by_kill_switch(method, path):
            return
        try:
            body = self._json_body() if method in {"POST", "PUT"} else {}
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        routes = [
            ("GET", r"^/api/status$", self._get_status),
            ("GET", r"^/api/control$", self._control_status),
            ("POST", r"^/api/control/kill-switch$", self._control_kill_switch),
            ("POST", r"^/api/control/resume$", self._control_resume),
            ("GET", r"^/api/companion/devices$", self._companion_list_devices),
            ("POST", r"^/api/companion/pairing/start$", self._companion_pairing_start),
            ("POST", r"^/api/companion/pairing/complete$", self._companion_pairing_complete),
            ("POST", r"^/api/companion/quick-capture$", self._companion_quick_capture),
            ("DELETE", r"^/api/companion/devices/([^/]+)$", self._companion_revoke_device),
            ("GET", r"^/api/conversations$", self._get_conversations),
            ("POST", r"^/api/chat$", self._post_chat),
            ("POST", r"^/api/voice/transcript$", self._voice_transcript),
            ("GET", r"^/api/memories$", self._list_memories),
            ("POST", r"^/api/memories$", self._create_memory),
            ("POST", r"^/api/memories/export$", self._export_memories),
            ("POST", r"^/api/memories/import$", self._import_memories),
            ("POST", r"^/api/memories/bulk-delete$", self._bulk_delete_memories),
            ("PUT", r"^/api/memories/([^/]+)$", self._update_memory),
            ("DELETE", r"^/api/memories/([^/]+)$", self._delete_memory),
            ("GET", r"^/api/tasks$", self._list_tasks),
            ("POST", r"^/api/tasks$", self._create_task),
            ("PUT", r"^/api/tasks/([^/]+)$", self._update_task),
            ("DELETE", r"^/api/tasks/([^/]+)$", self._delete_task),
            ("GET", r"^/api/agents$", self._list_agents),
            ("POST", r"^/api/agents$", self._create_agent),
            ("GET", r"^/api/agents/templates$", self._list_agent_templates),
            ("POST", r"^/api/agents/from-template$", self._spawn_from_template),
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
            ("GET", r"^/api/metrics$", self._get_metrics),
            ("GET", r"^/api/scheduler/status$", self._scheduler_status),
            ("POST", r"^/api/memories/prune$", self._prune_memories),
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
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
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

    def _route_companion_v2(
        self,
        method: str,
        path: str,
        query: dict[str, Any],
    ) -> None:
        try:
            body = self._json_body() if method in {"POST", "PUT"} else {}
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        routes = [
            (
                "POST",
                r"^/api/companion/v2/pairing/complete$",
                self._companion_v2_pairing_complete,
                False,
            ),
            (
                "GET",
                r"^/api/companion/v2/sync/snapshot$",
                self._companion_v2_sync_snapshot,
                True,
            ),
            (
                "GET",
                r"^/api/companion/v2/sync/events$",
                self._companion_v2_sync_events,
                True,
            ),
            (
                "POST",
                r"^/api/companion/v2/sync/ack$",
                self._companion_v2_sync_ack,
                True,
            ),
            (
                "POST",
                r"^/api/companion/v2/chat$",
                self._companion_v2_chat,
                True,
            ),
            (
                "GET",
                r"^/api/companion/v2/approvals$",
                self._companion_v2_approvals,
                True,
            ),
            (
                "POST",
                r"^/api/companion/v2/approvals/([^/]+)/decision$",
                self._companion_v2_approval_decision,
                True,
            ),
            (
                "GET",
                r"^/api/companion/v2/routines$",
                self._companion_v2_routines,
                True,
            ),
            (
                "POST",
                r"^/api/companion/v2/routines/([^/]+)/run$",
                self._companion_v2_routine_run,
                True,
            ),
            (
                "GET",
                r"^/api/companion/v2/memories$",
                self._companion_v2_memories,
                True,
            ),
            (
                "POST",
                r"^/api/companion/v2/memories$",
                self._companion_v2_memory_create,
                True,
            ),
            (
                "PUT",
                r"^/api/companion/v2/memories/([^/]+)$",
                self._companion_v2_memory_update,
                True,
            ),
            (
                "DELETE",
                r"^/api/companion/v2/memories/([^/]+)$",
                self._companion_v2_memory_delete,
                True,
            ),
            (
                "POST",
                r"^/api/companion/v2/quick-capture$",
                self._companion_v2_quick_capture,
                True,
            ),
            (
                "POST",
                r"^/api/companion/v2/presence$",
                self._companion_v2_presence,
                True,
            ),
            (
                "POST",
                r"^/api/companion/v2/control/kill-switch$",
                self._companion_v2_kill_switch,
                True,
            ),
        ]
        try:
            for route_method, pattern, handler, requires_auth in routes:
                match = re.match(pattern, path)
                if route_method != method or match is None:
                    continue
                if requires_auth:
                    context = self._verify_companion_v2_auth()
                    if context is None:
                        return
                    self._companion_device_context = context
                    if self._companion_v2_blocked_by_kill_switch(method, path):
                        return
                return handler(match.groups(), body, query)
            self._json_response({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
        except KeyError as exc:
            self._json_response({"error": f"Not found: {exc}"}, status=HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self._json_response({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self.app.audit.log(
                action_type="companion_v2_server_error",
                action_tier=0,
                tool_name="http_server",
                outcome="failed",
                error=str(exc),
            )
            self._json_response(
                {"error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _requires_owner_session(self, method: str, path: str) -> bool:
        if method == "GET":
            sensitive_gets = (
                "/api/audit",
                "/api/companion/devices",
                "/api/conversations",
                "/api/memories",
                "/api/tasks",
                "/api/agents",
                "/api/routines",
                "/api/settings",
                "/api/tools",
                "/api/secrets",
                "/api/dispatches",
                "/api/diagnostics",
                "/api/learning",
                "/api/provider",
            )
            if path == "/api/agents/templates":
                return False
            return any(
                path == prefix or path.startswith(f"{prefix}/")
                for prefix in sensitive_gets
            )
        if method not in {"POST", "PUT", "DELETE"}:
            return False
        if method == "POST" and path == "/api/control/kill-switch":
            return False
        if method == "POST" and path == "/api/companion/pairing/complete":
            return False
        if method == "POST" and path == "/api/companion/quick-capture":
            return False
        return True

    def _owner_session_allowed(self) -> bool:
        token = self.headers.get("X-Project-Q-Owner-Session", "").strip() or self._cookie_value(OWNER_SESSION_COOKIE)
        if not token:
            self._json_response({"error": "owner session is required"}, status=HTTPStatus.UNAUTHORIZED)
            return False
        try:
            self.app.owner_auth.verify_session(token)
        except PermissionError as exc:
            self._json_response({"error": str(exc)}, status=HTTPStatus.UNAUTHORIZED)
            return False
        return True

    def _blocked_by_kill_switch(self, method: str, path: str) -> bool:
        if method not in {"POST", "PUT", "DELETE"}:
            return False
        if path in {"/api/control/kill-switch", "/api/control/resume"}:
            return False
        if method == "DELETE" and re.match(r"^/api/companion/devices/[^/]+$", path):
            return False
        control = self.app.control.status()
        if not control["active"]:
            return False
        reason = control["reason"] or "Emergency stop activated"
        self._json_response({"error": f"kill switch active: {reason}"}, status=HTTPStatus.LOCKED)
        return True

    def _get_status(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response(
            {
                "project": self.app.config.project_name,
                "workspace_root": str(self.app.config.workspace_root),
                "data_root": str(self.app.config.data_root),
                "tool_count": len(self.app.tools.describe_all()),
                "status": "online",
                "reasoner": self.app.reasoner.status(),
                "control": self.app.control.status(),
                "runtime": self.app.runtime_metadata(),
            }
        )

    def _control_status(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response(self.app.control.status())

    def _control_kill_switch(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        payload = ControlRequest.model_validate(body)
        self._json_response(self.app.control.activate(reason=payload.reason, source=payload.source))

    def _control_resume(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        payload = ControlRequest.model_validate(body)
        self._json_response(self.app.control.resume(reason=payload.reason, source=payload.source))

    def _companion_list_devices(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response({"items": self.app.companion.list_devices()})

    def _companion_pairing_start(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        payload = CompanionPairingStartRequest.model_validate(body)
        offer = self.app.companion.start_pairing_v2(
            device_name=payload.device_name,
            platform=payload.platform,
        )
        direct_base_url = self._request_base_url()
        relay_base_url = None
        relay_token = None
        if self.app.relay_provisioner is not None:
            try:
                relay = self.app.relay_provisioner.provision_pair(
                    windows_device_id=offer["windows_device_id"],
                    phone_device_id=offer["device_id"],
                )
                relay_base_url = relay["relay_base_url"]
                relay_token = relay["phone_token"]
                self.app.ensure_relay_bridge(relay["windows_token"])
            except Exception as exc:
                self.app.audit.log(
                    action_type="companion_relay_provision",
                    action_tier=2,
                    tool_name="companion_relay",
                    outcome="failed",
                    approved_by_owner=True,
                    input_sources=["owner"],
                    error=str(exc),
                    metadata={"device_id": offer["device_id"]},
                )
        qr_payload = {
            "direct_base_url": direct_base_url,
            "relay_base_url": relay_base_url,
            "relay_token": relay_token,
            "offer": {
                "protocol_version": offer["protocol_version"],
                "device_id": offer["device_id"],
                "windows_device_id": offer["windows_device_id"],
                "windows_key_agreement_public_key": offer[
                    "windows_key_agreement_public_key"
                ],
                "pairing_token": offer["pairing_token"],
                "expires_at": offer["expires_at"],
            },
        }
        qr_payload_text = json.dumps(
            qr_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded_payload = base64.urlsafe_b64encode(
            qr_payload_text.encode("utf-8")
        ).decode("ascii").rstrip("=")
        pairing_uri = f"projectq://pair?payload={encoded_payload}"
        qr_data_url = None
        if qrcode is not None:
            qr_image = qrcode.make(pairing_uri)
            image_buffer = io.BytesIO()
            qr_image.save(image_buffer, format="PNG")
            qr_data_url = (
                "data:image/png;base64,"
                + base64.b64encode(image_buffer.getvalue()).decode("ascii")
            )
        response = {
            **offer,
            "direct_base_url": direct_base_url,
            "relay_base_url": relay_base_url,
            "relay_token": relay_token,
            "qr_payload": qr_payload_text,
            "pairing_uri": pairing_uri,
            "qr_available": qr_data_url is not None,
            "qr_data_url": qr_data_url,
        }
        self._json_response(
            response,
            status=HTTPStatus.CREATED,
            headers={"Cache-Control": "no-store"},
        )

    def _companion_pairing_complete(
        self,
        _args: tuple[str, ...],
        body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        payload = CompanionPairingCompleteRequest.model_validate(body)
        self._json_response(
            self.app.companion.complete_pairing(
                device_id=payload.device_id,
                pairing_secret=payload.pairing_secret,
            )
        )

    def _companion_quick_capture(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        payload = CompanionQuickCaptureRequest.model_validate(body)
        self._verify_companion_auth(payload.device_id)
        self._json_response(
            self.app.companion.ingest_quick_capture(payload.device_id, payload.envelope),
            status=HTTPStatus.CREATED,
        )

    def _companion_v2_pairing_complete(
        self,
        _args: tuple[str, ...],
        body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        payload = CompanionV2PairingCompleteRequest.model_validate(body)
        self._json_response(
            self.app.companion.complete_pairing_v2(
                device_id=payload.device_id,
                pairing_token=payload.pairing_token,
                key_agreement_public_key=payload.key_agreement_public_key,
                approval_public_key=payload.approval_public_key,
            )
        )

    def _companion_v2_sync_snapshot(
        self,
        _args: tuple[str, ...],
        _body: dict[str, Any],
        query: dict[str, Any],
    ) -> None:
        limits = {
            name: int(query.get(name, ["100"])[0])
            for name in (
                "conversations",
                "memories",
                "tasks",
                "agents",
                "routines",
                "approvals",
                "audit",
            )
            if name in query
        }
        self._json_response(
            self.app.sync.snapshot(
                self._companion_device_context["device_id"],
                limits=limits,
            )
        )

    def _companion_v2_presence(
        self,
        _args: tuple[str, ...],
        body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        payload = CompanionV2PresenceRequest.model_validate(body)
        self._json_response(
            self.app.companion.publish_presence(
                self._companion_device_context["device_id"],
                state=payload.state,
                ttl_seconds=payload.ttl_seconds,
            )
        )

    def _companion_v2_sync_events(
        self,
        _args: tuple[str, ...],
        _body: dict[str, Any],
        query: dict[str, Any],
    ) -> None:
        self._json_response(
            self.app.sync.pull(
                self._companion_device_context["device_id"],
                after_sequence=int(query.get("after_sequence", ["0"])[0]),
                limit=int(query.get("limit", ["200"])[0]),
            )
        )

    def _companion_v2_sync_ack(
        self,
        _args: tuple[str, ...],
        body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        payload = CompanionV2SyncAckRequest.model_validate(body)
        self._json_response(
            self.app.sync.acknowledge(
                self._companion_device_context["device_id"],
                sequence_number=payload.sequence_number,
            )
        )

    def _companion_v2_chat(
        self,
        _args: tuple[str, ...],
        body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        payload = CompanionV2ChatRequest.model_validate(body)
        response = self.app.conversations.respond(
            ChatRequest(message=payload.message, owner_approved=False),
            channel="iphone",
            input_sources=["owner", "iphone_companion", "reasoner"],
        )
        self.app.audit.log(
            action_type="companion_chat_turn",
            action_tier=0,
            tool_name="conversation",
            outcome="completed",
            input_sources=["iphone_companion"],
            metadata={
                "device_id": self._companion_device_context["device_id"],
                "created_action_request_ids": response.created_action_request_ids,
            },
        )
        self._json_response(response.model_dump())

    def _companion_v2_approvals(
        self,
        _args: tuple[str, ...],
        _body: dict[str, Any],
        query: dict[str, Any],
    ) -> None:
        self._json_response(
            {
                "items": self.app.approvals.list_pending(
                    limit=int(query.get("limit", ["100"])[0])
                )
            }
        )

    def _companion_v2_approval_decision(
        self,
        args: tuple[str, ...],
        body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        payload = CompanionV2ApprovalDecisionRequest.model_validate(body)
        action_request = self.app.approvals.decide(
            action_request_id=args[0],
            device_id=self._companion_device_context["device_id"],
            decision=payload.decision,
            timestamp=payload.timestamp,
            signature=payload.signature,
            biometric_backed=payload.biometric_backed,
        )
        execution = (
            self.app.approvals.execute_approved(args[0])
            if payload.decision == "approve"
            else action_request
        )
        self._json_response(
            {"request": action_request, "execution": execution}
        )

    def _companion_v2_routines(
        self,
        _args: tuple[str, ...],
        _body: dict[str, Any],
        query: dict[str, Any],
    ) -> None:
        limit = max(1, min(int(query.get("limit", ["100"])[0]), 200))
        self._json_response({"items": self.app.routines.list_all(limit=limit)})

    def _companion_v2_routine_run(
        self,
        args: tuple[str, ...],
        _body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        self._json_response(
            self.app.routine_runner.run(args[0], owner_approved=False)
        )

    def _companion_v2_memories(
        self,
        _args: tuple[str, ...],
        _body: dict[str, Any],
        query: dict[str, Any],
    ) -> None:
        search = str(query.get("q", [""])[0]).strip()
        limit = max(1, min(int(query.get("limit", ["100"])[0]), 200))
        items = (
            self.app.memory.search(search, limit=limit)
            if search
            else self.app.memory.list_all(limit=limit)
        )
        self._json_response({"items": items})

    def _companion_v2_memory_create(
        self,
        _args: tuple[str, ...],
        body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        payload = CompanionV2MemoryCreateRequest.model_validate(body)
        memory = self.app.memory.create(
            MemoryCreate(
                text=payload.text,
                kind=payload.kind,
                source="iphone_companion",
                confidence=payload.confidence,
                owner_confirmed=True,
                tags=payload.tags,
                metadata={
                    **payload.metadata,
                    "device_id": self._companion_device_context["device_id"],
                    "trust_zone": "zone_2_owner_device",
                },
            )
        )
        self._json_response(memory, status=HTTPStatus.CREATED)

    def _companion_v2_memory_update(
        self,
        args: tuple[str, ...],
        body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        payload = CompanionV2MemoryUpdateRequest.model_validate(body)
        self._json_response(
            self.app.memory.update(
                args[0],
                MemoryUpdate.model_validate(payload.model_dump(exclude_none=True)),
            )
        )

    def _companion_v2_memory_delete(
        self,
        args: tuple[str, ...],
        _body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        self.app.memory.delete(args[0])
        self._json_response({"deleted": args[0]})

    def _companion_v2_quick_capture(
        self,
        _args: tuple[str, ...],
        body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        payload = CompanionV2QuickCaptureRequest.model_validate(body)
        memory = self.app.memory.create(
            MemoryCreate(
                text=payload.text,
                kind="episodic",
                source="iphone_companion",
                confidence=0.75,
                owner_confirmed=False,
                tags=["iphone", "quick_capture", payload.capture_type],
                metadata={
                    **payload.metadata,
                    "device_id": self._companion_device_context["device_id"],
                    "trust_zone": "zone_2_owner_device",
                    "capture_type": payload.capture_type,
                },
            )
        )
        self._json_response(
            {"created_memory": memory, "delivery": "committed"},
            status=HTTPStatus.CREATED,
        )

    def _companion_v2_kill_switch(
        self,
        _args: tuple[str, ...],
        body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        payload = ControlRequest.model_validate(
            {
                "reason": body.get("reason", "iPhone emergency stop"),
                "source": "iphone_companion",
            }
        )
        self._json_response(
            self.app.control.activate(
                reason=payload.reason,
                source=payload.source,
            )
        )

    def _companion_v2_blocked_by_kill_switch(self, method: str, path: str) -> bool:
        if method not in {"POST", "PUT", "DELETE"}:
            return False
        allowed = {
            "/api/companion/v2/pairing/complete",
            "/api/companion/v2/sync/ack",
            "/api/companion/v2/presence",
            "/api/companion/v2/control/kill-switch",
        }
        if path in allowed or re.match(
            r"^/api/companion/v2/approvals/[^/]+/decision$",
            path,
        ):
            return False
        control = self.app.control.status()
        if not control["active"]:
            return False
        reason = control["reason"] or "Emergency stop activated"
        self._json_response(
            {"error": f"kill switch active: {reason}"},
            status=HTTPStatus.LOCKED,
        )
        return True

    def _companion_revoke_device(self, args: tuple[str, ...], _body: dict[str, Any], query: dict[str, Any]) -> None:
        reason = query.get("reason", ["owner revoked companion device"])[0]
        source = query.get("source", ["dashboard"])[0]
        self._json_response(self.app.companion.revoke_device(args[0], reason=reason, source=source))

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

    def _voice_transcript(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        payload = VoiceTranscriptRequest.model_validate(body)
        result = self.app.voice.ingest_transcript(
            transcript=payload.transcript,
            owner_approved=payload.owner_approved,
            source=payload.source,
        )
        response = result["response"]
        self._json_response(
            {
                "source": result["source"],
                "transcript": result["transcript"],
                "response": response.model_dump(),
            },
            status=HTTPStatus.CREATED,
        )

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

    def _export_memories(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        result = self.app.memory.export_all(limit=int(body.get("limit", 10000)))
        self.app.audit.log(
            action_type="memory_export",
            action_tier=1,
            tool_name="memory_service",
            outcome="completed",
            approved_by_owner=True,
            metadata={"memory_count": result["memory_count"]},
        )
        self._json_response(result)

    def _bulk_delete_memories(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        result = self.app.memory.bulk_delete(
            source=body.get("source"),
            kind=body.get("kind"),
            tag=body.get("tag"),
            older_than=body.get("older_than"),
            dry_run=bool(body.get("dry_run", True)),
            owner_confirmed=bool(body.get("owner_confirmed", False)),
            all_memories=bool(body.get("all_memories", False)),
        )
        self.app.audit.log(
            action_type="memory_bulk_delete",
            action_tier=3,
            tool_name="memory_service",
            outcome="completed" if result["deleted_count"] else "dry_run",
            approved_by_owner=bool(body.get("owner_confirmed", False)),
            metadata={
                "matched_count": result["matched_count"],
                "deleted_count": result["deleted_count"],
                "filters": result["filters"],
            },
        )
        self._json_response(result)

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
        task = self.app.tasks.update(args[0], TaskUpdate.model_validate(body))
        # Auto-reflect when a task is completed and the setting is enabled
        if task.get("status") == "completed":
            try:
                settings = self.app.settings.get_all()
                if settings.get("auto_reflect_on_tasks", False):
                    self.app.learning.reflect_on_task(args[0])
            except Exception:  # noqa: BLE001
                pass  # reflection is best-effort, never block the response
        self._json_response(task)

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
            input_sources=["owner"],
            tool_id=request.tool_id,
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

    def _list_agent_templates(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        self._json_response({"items": self.app.agents.list_templates()})

    def _spawn_from_template(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        template_id = str(body.get("template_id", "")).strip()
        goal_override = str(body.get("goal_override", "")).strip()
        if not template_id:
            self._json_response({"error": "template_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        agent = self.app.agents.spawn_from_template(template_id, goal_override=goal_override)
        self.app.audit.log(
            action_type="agent_create",
            action_tier=1,
            tool_name="agent_service",
            outcome="completed",
            approved_by_owner=True,
            metadata={"agent_id": agent["id"], "template_id": template_id},
        )
        self._json_response(agent, status=HTTPStatus.CREATED)

    def _get_metrics(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        try:
            metrics = self.app.learning.get_metrics()
        except AttributeError:
            with self.app.db.connection() as conn:
                agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
                task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                memory_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                routine_count = conn.execute("SELECT COUNT(*) FROM routines").fetchone()[0]
            metrics = {
                "agent_count": agent_count,
                "task_count": task_count,
                "memory_count": memory_count,
                "routine_count": routine_count,
            }
        self._json_response(metrics)

    def _scheduler_status(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        scheduler = getattr(self.app, "scheduler", None)
        if scheduler is None:
            self._json_response({"running": False, "scheduled_count": 0})
            return
        try:
            status = scheduler.status()
        except AttributeError:
            status = {"running": False, "scheduled_count": 0}
        self._json_response(status)

    def _prune_memories(self, _args: tuple[str, ...], _body: dict[str, Any], _query: dict[str, Any]) -> None:
        result = self.app.memory.prune_expired()
        self._json_response(result)

    def _stream_chat(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        payload = ChatRequest.model_validate(body)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        stream = self.app.conversations.respond_stream(payload)
        try:
            for event in stream:
                if event.token:
                    self._write_sse_data({"token": event.token})
                if event.response is None:
                    continue
                response = event.response
                self.app.audit.log(
                    action_type="chat_turn",
                    action_tier=0,
                    tool_name="conversation/stream",
                    outcome="completed",
                    metadata=response.model_dump(),
                    input_sources=["owner"],
                )
                self._write_sse_data(
                    {
                        "done": True,
                        "reply": response.reply,
                        "created_task_ids": response.created_task_ids,
                        "created_memory_ids": response.created_memory_ids,
                        "created_agent_ids": response.created_agent_ids,
                        "created_routine_ids": response.created_routine_ids,
                        "executed_tools": response.executed_tools,
                        "blocked_tools": response.blocked_tools,
                        "reasoning_mode": response.reasoning_mode,
                        "model_name": response.model_name,
                    }
                )
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        except Exception as exc:  # noqa: BLE001
            self.app.audit.log(
                action_type="stream_error",
                action_tier=0,
                tool_name="conversation/stream",
                outcome="failed",
                error=str(exc),
                input_sources=["owner"],
            )
            try:
                self._write_sse_data({"error": str(exc), "done": True})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    def _stream_companion_v2_chat(
        self,
        _args: tuple[str, ...],
        body: dict[str, Any],
        _query: dict[str, Any],
    ) -> None:
        payload = CompanionV2ChatRequest.model_validate(body)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        stream = self.app.conversations.respond_stream(
            ChatRequest(message=payload.message, owner_approved=False),
            channel="iphone",
            input_sources=["owner", "iphone_companion", "reasoner"],
        )
        try:
            for event in stream:
                if event.token:
                    self._write_sse_data({"token": event.token})
                if event.response is None:
                    continue
                response = event.response
                self.app.audit.log(
                    action_type="companion_chat_turn",
                    action_tier=0,
                    tool_name="conversation/stream",
                    outcome="completed",
                    input_sources=["iphone_companion"],
                    metadata={
                        "device_id": self._companion_device_context["device_id"],
                        "created_action_request_ids": response.created_action_request_ids,
                        "reasoning_mode": response.reasoning_mode,
                        "model_name": response.model_name,
                    },
                )
                self._write_sse_data(
                    {
                        "done": True,
                        "reply": response.reply,
                        "created_action_request_ids": response.created_action_request_ids,
                        "reasoning_mode": response.reasoning_mode,
                        "model_name": response.model_name,
                    }
                )
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        except Exception as exc:  # noqa: BLE001
            self.app.audit.log(
                action_type="companion_v2_stream_error",
                action_tier=0,
                tool_name="conversation/stream",
                outcome="failed",
                error=str(exc),
                input_sources=["iphone_companion"],
                metadata={
                    "device_id": self._companion_device_context["device_id"],
                },
            )
            try:
                self._write_sse_data({"error": str(exc), "done": True})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    def _write_sse_data(self, payload: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _import_memories(self, _args: tuple[str, ...], body: dict[str, Any], _query: dict[str, Any]) -> None:
        memories_data = body.get("memories", [])
        if not isinstance(memories_data, list):
            self._json_response({"error": "memories must be a list"}, status=HTTPStatus.BAD_REQUEST)
            return
        imported, failed = 0, 0
        for mem_data in memories_data[:5000]:
            try:
                self.app.memory.create(
                    MemoryCreate.model_validate({
                        "text": str(mem_data.get("text", "")).strip()[:5000],
                        "kind": str(mem_data.get("kind", "semantic")),
                        "source": "import",
                        "confidence": float(mem_data.get("confidence", 0.8)),
                        "owner_confirmed": False,
                        "tags": list(mem_data.get("tags") or []),
                        "metadata": dict(mem_data.get("metadata") or {}),
                    })
                )
                imported += 1
            except Exception:  # noqa: BLE001
                failed += 1
        self.app.audit.log(
            action_type="memory_import",
            action_tier=1,
            tool_name="memory_service",
            outcome="completed",
            approved_by_owner=True,
            metadata={"imported": imported, "failed": failed},
        )
        self._json_response({"imported": imported, "failed": failed}, status=HTTPStatus.CREATED)

    def _json_body(self) -> dict[str, Any]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length header") from exc
        if content_length > MAX_JSON_BODY_BYTES:
            self._last_body_bytes = b""
            raise ValueError("JSON body too large")
        if content_length == 0:
            self._last_body_bytes = b""
            return {}
        raw = self.rfile.read(content_length)
        self._last_body_bytes = raw
        try:
            decoded = raw.decode("utf-8")
            parsed = json.loads(decoded)
        except UnicodeDecodeError as exc:
            raise ValueError("invalid UTF-8 JSON body") from exc
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def _verify_companion_auth(self, device_id: str) -> None:
        parsed = urlparse(self.path)
        self.app.companion.verify_request(
            device_id=device_id,
            method=self.command,
            path=parsed.path,
            timestamp=self.headers.get("X-Project-Q-Timestamp", ""),
            body=getattr(self, "_last_body_bytes", b""),
            signature=self.headers.get("X-Project-Q-Signature", ""),
        )

    def _verify_companion_v2_auth(self) -> dict[str, Any] | None:
        required_headers = {
            "device_id": self.headers.get("X-Project-Q-Device-ID", "").strip(),
            "timestamp": self.headers.get("X-Project-Q-Timestamp", "").strip(),
            "request_nonce": self.headers.get("X-Project-Q-Request-Nonce", "").strip(),
            "sequence_number": self.headers.get("X-Project-Q-Sequence", "").strip(),
            "signature": self.headers.get("X-Project-Q-Signature", "").strip(),
        }
        if not all(required_headers.values()):
            self._json_response(
                {"error": "companion device authentication is required"},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return None
        parsed = urlparse(self.path)
        request_target = parsed.path
        if parsed.query:
            request_target += f"?{parsed.query}"
        return self.app.companion_auth.verify_request(
            device_id=required_headers["device_id"],
            method=self.command,
            path=request_target,
            timestamp=required_headers["timestamp"],
            request_nonce=required_headers["request_nonce"],
            sequence_number=int(required_headers["sequence_number"]),
            body=getattr(self, "_last_body_bytes", b""),
            signature=required_headers["signature"],
        )

    def _json_response(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _request_base_url(self) -> str:
        host_header = self.headers.get("Host", "").strip()
        if host_header:
            return f"http://{host_header}"
        host = self.app.config.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.app.config.port}"

    def _cookie_value(self, name: str) -> str:
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            if key == name:
                return value
        return ""

    def _owner_session_cookie_header(self) -> str:
        session = self.app.owner_auth.create_session(source="dashboard")
        max_age = max(1, self.app.owner_auth.session_ttl_hours * 3600)
        return (
            f"{OWNER_SESSION_COOKIE}={session['token']}; "
            f"Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"
        )

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
        if requested.name == "index.html":
            self.send_header("Set-Cookie", self._owner_session_cookie_header())
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
        server.RequestHandlerClass.app.shutdown_services()
        server.RequestHandlerClass.app.clear_runtime_metadata()
        server.server_close()


if __name__ == "__main__":
    main()
