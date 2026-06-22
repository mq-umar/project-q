from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class APNsRouteStore:
    def __init__(self, store, encryption_key: bytes | None = None) -> None:
        self.store = store
        self.encryption_key = encryption_key

    @property
    def available(self) -> bool:
        return (
            isinstance(self.encryption_key, bytes)
            and len(self.encryption_key) == 32
        )

    def set(
        self,
        *,
        device_id: str,
        device_token: str,
        environment: str,
        updated_at: str,
    ) -> dict[str, str]:
        if not self.available:
            raise RuntimeError("APNs route encryption is not configured")
        token = str(device_token).strip().lower()
        try:
            token_bytes = bytes.fromhex(token)
        except ValueError as exc:
            raise ValueError("invalid APNs device token") from exc
        if len(token_bytes) < 16 or len(token_bytes) > 256:
            raise ValueError("invalid APNs device token")
        normalized_environment = str(environment).strip().lower()
        if normalized_environment not in {"development", "production"}:
            raise ValueError("invalid APNs environment")
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self.encryption_key).encrypt(
            nonce,
            token.encode("ascii"),
            str(device_id).encode("utf-8"),
        )
        encoded = base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")
        with self.store.connection() as conn:
            conn.execute(
                """
                INSERT INTO relay_apns_routes (
                    device_id, token_hash, encrypted_token,
                    environment, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    token_hash = excluded.token_hash,
                    encrypted_token = excluded.encrypted_token,
                    environment = excluded.environment,
                    updated_at = excluded.updated_at
                """,
                (
                    device_id,
                    hashlib.sha256(token.encode("ascii")).hexdigest(),
                    encoded,
                    normalized_environment,
                    updated_at,
                ),
            )
        return {
            "device_id": str(device_id),
            "environment": normalized_environment,
            "updated_at": updated_at,
        }

    def get(self, device_id: str) -> dict[str, str] | None:
        if not self.available:
            return None
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM relay_apns_routes WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        if row is None:
            return None
        encrypted = base64.urlsafe_b64decode(str(row["encrypted_token"]))
        token = AESGCM(self.encryption_key).decrypt(
            encrypted[:12],
            encrypted[12:],
            str(device_id).encode("utf-8"),
        ).decode("ascii")
        return {
            "device_id": str(row["device_id"]),
            "device_token": token,
            "environment": str(row["environment"]),
            "updated_at": str(row["updated_at"]),
        }


class APNsAdapter(Protocol):
    def send_wake(
        self,
        *,
        recipient_device_id: str,
        message_kind: str,
        routing_id: str,
        collapse_id: str,
    ) -> None:
        ...


@dataclass(slots=True)
class DisabledAPNsAdapter:
    def send_wake(
        self,
        *,
        recipient_device_id: str,
        message_kind: str,
        routing_id: str,
        collapse_id: str,
    ) -> None:
        del recipient_device_id, message_kind, routing_id, collapse_id


@dataclass(slots=True)
class RecordingAPNsAdapter:
    requests: list[dict[str, str]] = field(default_factory=list)

    def send_wake(
        self,
        *,
        recipient_device_id: str,
        message_kind: str,
        routing_id: str,
        collapse_id: str,
    ) -> None:
        self.requests.append(
            {
                "recipient_device_id": recipient_device_id,
                "message_kind": message_kind,
                "routing_id": routing_id,
                "collapse_id": collapse_id,
            }
        )


class ProductionAPNsAdapter:
    def __init__(
        self,
        *,
        route_store: APNsRouteStore,
        team_id: str,
        key_id: str,
        bundle_id: str,
        private_key_pem: bytes,
        http_client=None,
    ) -> None:
        self.route_store = route_store
        self.team_id = str(team_id).strip()
        self.key_id = str(key_id).strip()
        self.bundle_id = str(bundle_id).strip()
        if not self.team_id or not self.key_id or not self.bundle_id:
            raise ValueError("APNs team, key, and bundle IDs are required")
        private_key = serialization.load_pem_private_key(
            private_key_pem,
            password=None,
        )
        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
            private_key.curve,
            ec.SECP256R1,
        ):
            raise ValueError("APNs private key must be a P-256 key")
        self.private_key = private_key
        self.http_client = http_client or httpx.Client(
            http2=True,
            timeout=10.0,
        )
        self._cached_jwt = ""
        self._cached_jwt_issued_at = 0

    def send_wake(
        self,
        *,
        recipient_device_id: str,
        message_kind: str,
        routing_id: str,
        collapse_id: str,
    ) -> None:
        route = self.route_store.get(recipient_device_id)
        if route is None:
            return
        approval = str(message_kind) == "approval"
        host = (
            "api.sandbox.push.apple.com"
            if route["environment"] == "development"
            else "api.push.apple.com"
        )
        payload = {
            "aps": (
                {
                    "alert": {
                        "title": "Project Q",
                        "body": "An action needs your review.",
                    },
                    "category": "PROJECT_Q_APPROVAL",
                    "sound": "default",
                }
                if approval
                else {"content-available": 1}
            ),
            "project_q": {
                "message_kind": str(message_kind)[:40],
                "routing_id": str(routing_id)[:200],
            },
            "route": "approvals" if approval else "activity",
        }
        response = self.http_client.post(
            f"https://{host}/3/device/{route['device_token']}",
            headers={
                "authorization": f"bearer {self._provider_token()}",
                "apns-topic": self.bundle_id,
                "apns-push-type": "alert" if approval else "background",
                "apns-priority": "10" if approval else "5",
                "apns-collapse-id": str(collapse_id)[:64],
            },
            json=payload,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"APNs rejected notification with HTTP {response.status_code}"
            )

    def _provider_token(self) -> str:
        now = int(time.time())
        if self._cached_jwt and now - self._cached_jwt_issued_at < 50 * 60:
            return self._cached_jwt
        header = self._b64json({"alg": "ES256", "kid": self.key_id})
        claims = self._b64json({"iss": self.team_id, "iat": now})
        signing_input = f"{header}.{claims}".encode("ascii")
        der_signature = self.private_key.sign(
            signing_input,
            ec.ECDSA(hashes.SHA256()),
        )
        r, s = decode_dss_signature(der_signature)
        raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        signature = base64.urlsafe_b64encode(raw_signature).decode("ascii").rstrip("=")
        self._cached_jwt = f"{header}.{claims}.{signature}"
        self._cached_jwt_issued_at = now
        return self._cached_jwt

    @staticmethod
    def _b64json(value: dict[str, object]) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
