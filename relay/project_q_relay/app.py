from __future__ import annotations

import asyncio
import hmac
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, ConfigDict, Field

from project_q_relay.apns import ProductionAPNsAdapter
from project_q_relay.config import RelayConfig
from project_q_relay.service import RelayService
from project_q_relay.storage import RelayStore


class PresenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=1, max_length=20)
    ttl_seconds: int = Field(default=120, ge=15, le=120)


class RevocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="owner revoked relay device", max_length=500)


class ProvisionDeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern="^(windows|phone)$")
    ttl_seconds: int = Field(default=30 * 24 * 60 * 60, ge=60, le=365 * 24 * 60 * 60)


class APNsRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_token: str = Field(min_length=32, max_length=512)
    environment: str = Field(pattern="^(development|production)$")


def create_relay_app(
    *,
    service: RelayService | None = None,
    config: RelayConfig | None = None,
) -> FastAPI:
    resolved_config = config or RelayConfig.from_environment()
    if resolved_config.bootstrap_token and len(resolved_config.bootstrap_token) < 32:
        raise ValueError("relay bootstrap token must contain at least 32 characters")
    if service is None:
        relay = RelayService(
            RelayStore(resolved_config.database_path),
            max_envelope_bytes=resolved_config.max_envelope_bytes,
            envelope_retention_seconds=resolved_config.envelope_retention_seconds,
            apns_route_key=resolved_config.apns_route_key,
        )
        apns_values = (
            resolved_config.apns_team_id,
            resolved_config.apns_key_id,
            resolved_config.apns_bundle_id,
            resolved_config.apns_private_key_file,
        )
        if all(apns_values) and relay.apns_routes.available:
            relay.apns_adapter = ProductionAPNsAdapter(
                route_store=relay.apns_routes,
                team_id=resolved_config.apns_team_id,
                key_id=resolved_config.apns_key_id,
                bundle_id=resolved_config.apns_bundle_id,
                private_key_pem=resolved_config.apns_private_key_file.read_bytes(),
            )
    else:
        relay = service
    app = FastAPI(
        title="Project Q Ciphertext Relay",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )
    app.state.relay_service = relay

    def bearer_identity(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="relay bearer token is required",
            )
        try:
            return relay.authenticate(authorization[7:].strip())
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc

    def bearer_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="relay bearer token is required",
            )
        token = authorization[7:].strip()
        try:
            relay.authenticate(token)
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc
        return token

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "project-q-relay"}

    @app.post(
        "/v1/admin/devices",
        status_code=status.HTTP_201_CREATED,
        include_in_schema=False,
    )
    def provision_device(
        payload: ProvisionDeviceRequest,
        bootstrap: Annotated[
            str | None,
            Header(alias="X-Project-Q-Relay-Bootstrap"),
        ] = None,
    ) -> dict[str, Any]:
        configured = resolved_config.bootstrap_token
        if not configured:
            raise HTTPException(status_code=404, detail="not found")
        if not bootstrap or not hmac.compare_digest(bootstrap, configured):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid relay bootstrap credential",
            )
        return relay.register_device(
            owner_id=resolved_config.owner_id,
            device_id=payload.device_id,
            role=payload.role,
            ttl_seconds=payload.ttl_seconds,
        )

    @app.post("/v1/envelopes", status_code=status.HTTP_201_CREATED)
    def upload_envelope(
        envelope: dict[str, Any],
        token: str = Depends(bearer_token),
    ) -> dict[str, Any]:
        try:
            return relay.upload_envelope(token=token, envelope=envelope)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/envelopes")
    def pull_envelopes(
        token: str = Depends(bearer_token),
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> dict[str, Any]:
        return relay.pull_envelopes(token=token, limit=limit)

    @app.post("/v1/envelopes/{message_id}/ack")
    def acknowledge_envelope(
        message_id: str,
        token: str = Depends(bearer_token),
    ) -> dict[str, Any]:
        try:
            return relay.acknowledge_envelope(
                token=token,
                message_id=message_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="message not found") from exc

    @app.post("/v1/presence")
    def publish_presence(
        payload: PresenceRequest,
        token: str = Depends(bearer_token),
    ) -> dict[str, Any]:
        try:
            return relay.publish_presence(
                token=token,
                state=payload.state,
                ttl_seconds=payload.ttl_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/apns/register")
    def register_apns_route(
        payload: APNsRegistrationRequest,
        token: str = Depends(bearer_token),
    ) -> dict[str, Any]:
        try:
            return relay.register_apns_route(
                token=token,
                device_token=payload.device_token,
                environment=payload.environment,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/presence")
    def list_presence(
        token: str = Depends(bearer_token),
    ) -> dict[str, Any]:
        return relay.list_presence(token=token)

    @app.post("/v1/devices/{device_id}/revoke")
    def revoke_device(
        device_id: str,
        payload: RevocationRequest,
        identity: dict[str, Any] = Depends(bearer_identity),
    ) -> dict[str, Any]:
        try:
            return relay.revoke_device(
                actor_device_id=identity["device_id"],
                target_device_id=device_id,
                reason=payload.reason,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="device not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.websocket("/v1/ws")
    async def envelope_socket(websocket: WebSocket, token: str) -> None:
        try:
            relay.authenticator.authenticate(token)
        except PermissionError:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        sent_message_ids: set[str] = set()
        try:
            while True:
                try:
                    relay.authenticator.authenticate(token)
                except PermissionError:
                    await websocket.close(code=4403)
                    return
                inbox = relay.pull_envelopes(token=token, limit=200)
                for envelope in inbox["items"]:
                    message_id = str(envelope["message_id"])
                    if message_id in sent_message_ids:
                        continue
                    await websocket.send_json(
                        {"type": "envelope", "envelope": envelope}
                    )
                    sent_message_ids.add(message_id)
                await asyncio.sleep(0.05)
        except WebSocketDisconnect:
            return

    return app
