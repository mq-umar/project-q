from __future__ import annotations

import base64
import ipaddress
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlparse

from project_q.models import (
    ChatRequest,
    CompanionV2ApprovalDecisionRequest,
    CompanionV2ChatRequest,
    CompanionV2MemoryCreateRequest,
    CompanionV2MemoryUpdateRequest,
    CompanionV2PresenceRequest,
    CompanionV2QuickCaptureRequest,
    CompanionV2SyncAckRequest,
    ControlRequest,
    MemoryCreate,
    MemoryUpdate,
    utc_now,
)
from project_q.services.companion_crypto import CompanionCryptoService


class RelayClient(Protocol):
    def upload(self, token: str, envelope: dict[str, Any]) -> dict[str, Any]:
        ...

    def pull(self, token: str, limit: int = 200) -> dict[str, Any]:
        ...

    def acknowledge(self, token: str, message_id: str) -> dict[str, Any]:
        ...


RelayCommandHandler = Callable[[str, str, str, bytes], tuple[int, bytes]]


class CompanionRelayBridgeService:
    def __init__(
        self,
        *,
        db,
        protocol_service,
        relay_client: RelayClient,
        windows_token: str,
        windows_device_id: str,
        command_handler: RelayCommandHandler,
        crypto_service: CompanionCryptoService | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.db = db
        self.protocol_service = protocol_service
        self.relay_client = relay_client
        self.windows_token = str(windows_token)
        self.windows_device_id = str(windows_device_id)
        self.command_handler = command_handler
        self.crypto = crypto_service or CompanionCryptoService()
        self.poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="project-q-relay-bridge",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=max(2.0, self.poll_interval_seconds * 2))

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                pass
            self._stop_event.wait(self.poll_interval_seconds)

    def poll_once(self, *, limit: int = 200) -> dict[str, int]:
        inbox = self.relay_client.pull(self.windows_token, limit=limit)
        result = {
            "received": 0,
            "processed": 0,
            "redelivered": 0,
            "rejected": 0,
        }
        for envelope in inbox.get("items", []):
            result["received"] += 1
            try:
                outcome = self._process_envelope(envelope)
            except (KeyError, PermissionError, TypeError, ValueError):
                result["rejected"] += 1
                self._acknowledge(str(envelope.get("message_id", "")))
                continue
            result[outcome] += 1
        return result

    def _process_envelope(self, envelope: dict[str, Any]) -> str:
        request_message_id = str(envelope["message_id"])
        device_id = str(envelope["sender_device_id"])
        if (
            envelope.get("event_kind") != "companion_command"
            or envelope.get("recipient_device_id") != self.windows_device_id
        ):
            raise ValueError("unsupported relay envelope")

        existing = self._existing_command(request_message_id)
        if existing is not None:
            response_text = existing["response_envelope_json"]
            if not response_text:
                raise PermissionError("relay command is already being processed")
            response_envelope = json.loads(response_text)
            self.relay_client.upload(self.windows_token, response_envelope)
            self._acknowledge(request_message_id)
            return "redelivered"

        keys = self.protocol_service.device_keys(device_id)
        command = self.crypto.open(
            key=keys["receive_key"],
            envelope=envelope,
            expected_sender=device_id,
            expected_recipient=self.windows_device_id,
        )
        command_id, method, path, body = self._validated_command(command)
        request_sequence = int(envelope["sequence_number"])
        retried = self._existing_logical_command(device_id, command_id)
        if retried is not None:
            self._accept_retry_sequence(device_id, request_sequence)
            response_envelope = self._build_response(
                device_id=device_id,
                request_message_id=request_message_id,
                status_code=int(retried["response_status"]),
                response_body=base64.b64decode(
                    str(retried["response_body_base64"]),
                    validate=True,
                ),
            )
            self.relay_client.upload(self.windows_token, response_envelope)
            self._acknowledge(request_message_id)
            return "redelivered"

        self._claim_command(
            request_message_id=request_message_id,
            device_id=device_id,
            command_id=command_id,
            request_sequence=request_sequence,
        )

        try:
            status_code, response_body = self.command_handler(
                device_id,
                method,
                path,
                body,
            )
            status_code = int(status_code)
            if not 100 <= status_code <= 599:
                raise ValueError("invalid relay command status")
            if not isinstance(response_body, bytes):
                raise TypeError("relay command response body must be bytes")
        except Exception:
            status_code = 500
            response_body = b'{"error":"relay command failed"}'

        response_envelope = self._build_response(
            device_id=device_id,
            request_message_id=request_message_id,
            status_code=status_code,
            response_body=response_body,
        )
        self._complete_command(
            request_message_id,
            response_envelope,
            status_code=status_code,
            response_body=response_body,
        )
        self.relay_client.upload(self.windows_token, response_envelope)
        self._acknowledge(request_message_id)
        return "processed"

    def _existing_command(self, request_message_id: str):
        with self.db.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM companion_relay_commands
                WHERE request_message_id = ?
                """,
                (request_message_id,),
            ).fetchone()

    def _existing_logical_command(self, device_id: str, command_id: str):
        with self.db.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM companion_relay_commands
                WHERE device_id = ? AND command_id = ? AND status = 'completed'
                """,
                (device_id, command_id),
            ).fetchone()

    def _claim_command(
        self,
        *,
        request_message_id: str,
        device_id: str,
        command_id: str,
        request_sequence: int,
    ) -> None:
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            device = conn.execute(
                """
                SELECT status, protocol_version, legacy_repair_required,
                       relay_receive_sequence
                FROM companion_devices
                WHERE id = ?
                """,
                (device_id,),
            ).fetchone()
            if (
                device is None
                or device["status"] != "paired"
                or int(device["protocol_version"]) != 2
                or bool(device["legacy_repair_required"])
            ):
                raise PermissionError("companion relay device is not active")
            if request_sequence <= int(device["relay_receive_sequence"]):
                raise PermissionError("companion relay sequence was already used")
            conn.execute(
                """
                INSERT INTO companion_relay_commands (
                    request_message_id, device_id, command_id, request_sequence,
                    response_envelope_json, response_status,
                    response_body_base64, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, 'processing', ?, ?)
                """,
                (
                    request_message_id,
                    device_id,
                    command_id,
                    request_sequence,
                    now,
                    now,
                ),
            )
            cursor = conn.execute(
                """
                UPDATE companion_devices
                SET relay_receive_sequence = ?, last_seen_at = ?
                WHERE id = ? AND relay_receive_sequence < ?
                """,
                (request_sequence, now, device_id, request_sequence),
            )
            if cursor.rowcount != 1:
                raise PermissionError("companion relay sequence conflict")

    def _accept_retry_sequence(
        self,
        device_id: str,
        request_sequence: int,
    ) -> None:
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE companion_devices
                SET relay_receive_sequence = ?, last_seen_at = ?
                WHERE id = ? AND relay_receive_sequence < ?
                """,
                (request_sequence, utc_now(), device_id, request_sequence),
            )
            if cursor.rowcount != 1:
                raise PermissionError("companion relay sequence was already used")

    def _next_send_sequence(self, device_id: str) -> int:
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT send_sequence FROM companion_devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            if row is None:
                raise PermissionError("unknown companion relay device")
            sequence = int(row["send_sequence"]) + 1
            conn.execute(
                "UPDATE companion_devices SET send_sequence = ? WHERE id = ?",
                (sequence, device_id),
            )
        return sequence

    def _build_response(
        self,
        *,
        device_id: str,
        request_message_id: str,
        status_code: int,
        response_body: bytes,
    ) -> dict[str, Any]:
        keys = self.protocol_service.device_keys(device_id)
        return self.crypto.seal(
            key=keys["send_key"],
            sender_device_id=self.windows_device_id,
            recipient_device_id=device_id,
            event_kind="companion_response",
            sequence_number=self._next_send_sequence(device_id),
            payload={
                "request_message_id": request_message_id,
                "status_code": int(status_code),
                "body": base64.b64encode(response_body).decode("ascii"),
            },
        )

    def _complete_command(
        self,
        request_message_id: str,
        response_envelope: dict[str, Any],
        *,
        status_code: int,
        response_body: bytes,
    ) -> None:
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE companion_relay_commands
                SET response_envelope_json = ?,
                    response_status = ?,
                    response_body_base64 = ?,
                    status = 'completed',
                    updated_at = ?
                WHERE request_message_id = ? AND status = 'processing'
                """,
                (
                    json.dumps(
                        response_envelope,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    int(status_code),
                    base64.b64encode(response_body).decode("ascii"),
                    utc_now(),
                    request_message_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PermissionError("relay command state changed unexpectedly")

    def _acknowledge(self, message_id: str) -> None:
        if message_id:
            self.relay_client.acknowledge(self.windows_token, message_id)

    @staticmethod
    def _validated_command(
        command: dict[str, Any],
    ) -> tuple[str, str, str, bytes]:
        if set(command) != {"command_id", "method", "path", "body"}:
            raise ValueError("invalid companion relay command")
        command_id = str(command["command_id"]).strip()
        if not command_id or len(command_id) > 200:
            raise ValueError("invalid companion relay command ID")
        method = str(command["method"]).upper()
        path = str(command["path"])
        if method not in {"GET", "POST", "PUT", "DELETE"}:
            raise ValueError("unsupported companion relay method")
        if (
            not path.startswith("/api/companion/v2/")
            or path == "/api/companion/v2/pairing/complete"
            or len(path) > 2000
        ):
            raise ValueError("unsupported companion relay path")
        try:
            body = base64.b64decode(str(command["body"]), validate=True)
        except ValueError as exc:
            raise ValueError("invalid companion relay body") from exc
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("companion relay body is too large")
        return command_id, method, path, body


class CompanionRelayCommandDispatcher:
    def __init__(self, application) -> None:
        self.app = application

    def handle(
        self,
        device_id: str,
        method: str,
        request_target: str,
        raw_body: bytes,
    ) -> tuple[int, bytes]:
        parsed = urlparse(request_target)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            body = self._json_body(raw_body)
            if self._blocked_by_kill_switch(method, path):
                control = self.app.control.status()
                reason = control["reason"] or "Emergency stop activated"
                return self._response(
                    {"error": f"kill switch active: {reason}"},
                    423,
                )
            payload, status = self._dispatch(
                device_id=device_id,
                method=method,
                path=path,
                body=body,
                query=query,
            )
            return self._response(payload, status)
        except KeyError as exc:
            return self._response({"error": f"Not found: {exc}"}, 404)
        except PermissionError as exc:
            return self._response({"error": str(exc)}, 403)
        except ValueError as exc:
            return self._response({"error": str(exc)}, 400)
        except Exception:
            return self._response({"error": "relay command failed"}, 500)

    def _dispatch(
        self,
        *,
        device_id: str,
        method: str,
        path: str,
        body: dict[str, Any],
        query: dict[str, list[str]],
    ) -> tuple[dict[str, Any], int]:
        if method == "GET" and path == "/api/companion/v2/sync/snapshot":
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
            return self.app.sync.snapshot(device_id, limits=limits), 200
        if method == "GET" and path == "/api/companion/v2/sync/events":
            return (
                self.app.sync.pull(
                    device_id,
                    after_sequence=int(query.get("after_sequence", ["0"])[0]),
                    limit=int(query.get("limit", ["200"])[0]),
                ),
                200,
            )
        if method == "POST" and path == "/api/companion/v2/sync/ack":
            payload = CompanionV2SyncAckRequest.model_validate(body)
            return (
                self.app.sync.acknowledge(
                    device_id,
                    sequence_number=payload.sequence_number,
                ),
                200,
            )
        if method == "POST" and path == "/api/companion/v2/chat":
            payload = CompanionV2ChatRequest.model_validate(body)
            response = self.app.conversations.respond(
                ChatRequest(message=payload.message, owner_approved=False),
                channel="iphone",
                input_sources=["owner", "iphone_companion", "reasoner"],
            )
            return response.model_dump(), 200
        if method == "GET" and path == "/api/companion/v2/approvals":
            return (
                {
                    "items": self.app.approvals.list_pending(
                        limit=int(query.get("limit", ["100"])[0])
                    )
                },
                200,
            )
        approval_match = re.match(
            r"^/api/companion/v2/approvals/([^/]+)/decision$",
            path,
        )
        if method == "POST" and approval_match:
            payload = CompanionV2ApprovalDecisionRequest.model_validate(body)
            action_id = approval_match.group(1)
            request = self.app.approvals.decide(
                action_request_id=action_id,
                device_id=device_id,
                decision=payload.decision,
                timestamp=payload.timestamp,
                signature=payload.signature,
                biometric_backed=payload.biometric_backed,
            )
            execution = (
                self.app.approvals.execute_approved(action_id)
                if payload.decision == "approve"
                else request
            )
            return {"request": request, "execution": execution}, 200
        if method == "GET" and path == "/api/companion/v2/routines":
            limit = max(1, min(int(query.get("limit", ["100"])[0]), 200))
            return {"items": self.app.routines.list_all(limit=limit)}, 200
        routine_match = re.match(
            r"^/api/companion/v2/routines/([^/]+)/run$",
            path,
        )
        if method == "POST" and routine_match:
            return (
                self.app.routine_runner.run(
                    routine_match.group(1),
                    owner_approved=False,
                ),
                200,
            )
        if method == "GET" and path == "/api/companion/v2/memories":
            search = str(query.get("q", [""])[0]).strip()
            limit = max(1, min(int(query.get("limit", ["100"])[0]), 200))
            items = (
                self.app.memory.search(search, limit=limit)
                if search
                else self.app.memory.list_all(limit=limit)
            )
            return {"items": items}, 200
        if method == "POST" and path == "/api/companion/v2/memories":
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
                        "device_id": device_id,
                        "trust_zone": "zone_2_owner_device",
                    },
                )
            )
            return memory, 201
        memory_match = re.match(
            r"^/api/companion/v2/memories/([^/]+)$",
            path,
        )
        if method == "PUT" and memory_match:
            payload = CompanionV2MemoryUpdateRequest.model_validate(body)
            return (
                self.app.memory.update(
                    memory_match.group(1),
                    MemoryUpdate.model_validate(
                        payload.model_dump(exclude_none=True)
                    ),
                ),
                200,
            )
        if method == "DELETE" and memory_match:
            memory_id = memory_match.group(1)
            self.app.memory.delete(memory_id)
            return {"deleted": memory_id}, 200
        if method == "POST" and path == "/api/companion/v2/quick-capture":
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
                        "device_id": device_id,
                        "trust_zone": "zone_2_owner_device",
                        "capture_type": payload.capture_type,
                    },
                )
            )
            return {"created_memory": memory, "delivery": "committed"}, 201
        if method == "POST" and path == "/api/companion/v2/presence":
            payload = CompanionV2PresenceRequest.model_validate(body)
            return (
                self.app.companion.publish_presence(
                    device_id,
                    state=payload.state,
                    ttl_seconds=payload.ttl_seconds,
                ),
                200,
            )
        if method == "POST" and path == "/api/companion/v2/control/kill-switch":
            payload = ControlRequest.model_validate(
                {
                    "reason": body.get("reason", "iPhone emergency stop"),
                    "source": "iphone_companion",
                }
            )
            return (
                self.app.control.activate(
                    reason=payload.reason,
                    source=payload.source,
                ),
                200,
            )
        raise KeyError(path)

    def _blocked_by_kill_switch(self, method: str, path: str) -> bool:
        if method not in {"POST", "PUT", "DELETE"}:
            return False
        allowed = {
            "/api/companion/v2/sync/ack",
            "/api/companion/v2/presence",
            "/api/companion/v2/control/kill-switch",
        }
        if path in allowed or re.match(
            r"^/api/companion/v2/approvals/[^/]+/decision$",
            path,
        ):
            return False
        return bool(self.app.control.status()["active"])

    @staticmethod
    def _json_body(raw_body: bytes) -> dict[str, Any]:
        if not raw_body:
            return {}
        parsed = json.loads(raw_body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    @staticmethod
    def _response(payload: dict[str, Any], status: int) -> tuple[int, bytes]:
        return (
            int(status),
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
                "utf-8"
            ),
        )


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        return None


class RelayHTTPClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 15.0) -> None:
        self.base_url = self._validated_base_url(base_url)
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self._opener = urllib.request.build_opener(_RejectRedirectHandler())

    def provision(
        self,
        bootstrap_token: str,
        *,
        device_id: str,
        role: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/admin/devices",
            headers={"X-Project-Q-Relay-Bootstrap": bootstrap_token},
            payload={"device_id": device_id, "role": role},
        )

    def upload(self, token: str, envelope: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/envelopes",
            headers={"Authorization": f"Bearer {token}"},
            payload=envelope,
        )

    def pull(self, token: str, limit: int = 200) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/envelopes?limit={max(1, min(int(limit), 500))}",
            headers={"Authorization": f"Bearer {token}"},
        )

    def acknowledge(self, token: str, message_id: str) -> dict[str, Any]:
        safe_message_id = urllib.parse.quote(str(message_id), safe="")
        return self._request(
            "POST",
            f"/v1/envelopes/{safe_message_id}/ack",
            headers={"Authorization": f"Bearer {token}"},
            payload={},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        request_headers = {"Accept": "application/json", **headers}
        if payload is not None:
            data = json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener.open(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_text = exc.read().decode("utf-8", errors="replace")
            raise PermissionError(
                f"relay request failed with HTTP {exc.code}: {error_text[:500]}"
            ) from exc
        if not isinstance(decoded, dict):
            raise ValueError("relay response must be an object")
        return decoded

    @staticmethod
    def _validated_base_url(value: str) -> str:
        parsed = urlparse(str(value).strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid relay base URL")
        if parsed.scheme == "http":
            host = parsed.hostname.lower()
            allowed = host == "localhost" or host.endswith(".local")
            if not allowed:
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    address = None
                allowed = bool(
                    address
                    and (
                        address.is_loopback
                        or address.is_private
                        or address.is_link_local
                    )
                )
            if not allowed:
                raise ValueError("remote relay URLs must use HTTPS")
        return str(value).strip().rstrip("/")


class RelayProvisioningService:
    WINDOWS_TOKEN_SECRET = "companion_relay_windows_token_v1"

    def __init__(
        self,
        *,
        relay_base_url: str,
        bootstrap_token: str,
        relay_client,
        vault_service,
    ) -> None:
        self.relay_base_url = str(relay_base_url).rstrip("/")
        self.bootstrap_token = str(bootstrap_token)
        self.relay_client = relay_client
        self.vault_service = vault_service

    def provision_pair(
        self,
        *,
        windows_device_id: str,
        phone_device_id: str,
    ) -> dict[str, str]:
        try:
            windows_token = self.vault_service.get_secret(
                self.WINDOWS_TOKEN_SECRET
            )
        except KeyError:
            registration = self.relay_client.provision(
                self.bootstrap_token,
                device_id=windows_device_id,
                role="windows",
            )
            windows_token = str(registration["token"])
            self.vault_service.set_secret(
                self.WINDOWS_TOKEN_SECRET,
                windows_token,
                "Project Q Windows relay bearer token",
            )
        phone = self.relay_client.provision(
            self.bootstrap_token,
            device_id=phone_device_id,
            role="phone",
        )
        return {
            "relay_base_url": self.relay_base_url,
            "windows_token": windows_token,
            "phone_token": str(phone["token"]),
        }
