from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from project_q_relay.apns import APNsRouteStore, DisabledAPNsAdapter
from project_q_relay.auth import RelayAuthenticator


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class RelayService:
    def __init__(
        self,
        store,
        *,
        max_envelope_bytes: int = 512 * 1024,
        envelope_retention_seconds: int = 7 * 24 * 60 * 60,
        apns_adapter=None,
        apns_route_key: bytes | None = None,
    ) -> None:
        self.store = store
        self.authenticator = RelayAuthenticator(store)
        self.max_envelope_bytes = max(1024, int(max_envelope_bytes))
        self.envelope_retention_seconds = max(
            60,
            int(envelope_retention_seconds),
        )
        self.apns_adapter = apns_adapter or DisabledAPNsAdapter()
        self.apns_routes = APNsRouteStore(store, apns_route_key)

    def register_device(
        self,
        *,
        owner_id: str,
        device_id: str,
        role: str,
        ttl_seconds: int = 30 * 24 * 60 * 60,
    ) -> dict[str, Any]:
        normalized_role = str(role).strip().lower()
        if normalized_role not in {"windows", "phone"}:
            raise ValueError("relay device role must be windows or phone")
        owner = str(owner_id).strip()
        device = str(device_id).strip()
        if not owner or not device:
            raise ValueError("owner ID and device ID are required")
        raw_token = secrets.token_bytes(32)
        token = base64.urlsafe_b64encode(raw_token).decode("ascii").rstrip("=")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = _now()
        expires_at = now + timedelta(
            seconds=max(60, min(int(ttl_seconds), 365 * 24 * 60 * 60))
        )
        with self.store.connection() as conn:
            conn.execute(
                """
                INSERT INTO relay_devices (
                    device_id, owner_id, role, token_hash,
                    token_expires_at, created_at, last_seen_at,
                    revoked_at, revocation_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, '')
                ON CONFLICT(device_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    role = excluded.role,
                    token_hash = excluded.token_hash,
                    token_expires_at = excluded.token_expires_at,
                    created_at = excluded.created_at,
                    last_seen_at = excluded.last_seen_at,
                    revoked_at = NULL,
                    revocation_reason = ''
                """,
                (
                    device,
                    owner,
                    normalized_role,
                    token_hash,
                    _timestamp(expires_at),
                    _timestamp(now),
                    _timestamp(now),
                ),
            )
        return {
            "device_id": device,
            "owner_id": owner,
            "role": normalized_role,
            "token": token,
            "expires_at": _timestamp(expires_at),
        }

    def authenticate(self, token: str) -> dict[str, Any]:
        identity = self.authenticator.authenticate(token)
        with self.store.connection() as conn:
            conn.execute(
                "UPDATE relay_devices SET last_seen_at = ? WHERE device_id = ?",
                (_timestamp(_now()), identity["device_id"]),
            )
        return identity

    def revoke_device(
        self,
        *,
        actor_device_id: str,
        target_device_id: str,
        reason: str,
    ) -> dict[str, Any]:
        now = _timestamp(_now())
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = conn.execute(
                "SELECT * FROM relay_devices WHERE device_id = ?",
                (actor_device_id,),
            ).fetchone()
            target = conn.execute(
                "SELECT * FROM relay_devices WHERE device_id = ?",
                (target_device_id,),
            ).fetchone()
            if actor is None or target is None:
                raise KeyError(target_device_id)
            if actor["owner_id"] != target["owner_id"]:
                raise PermissionError("relay devices do not share an owner")
            if actor["role"] != "windows" and actor_device_id != target_device_id:
                raise PermissionError("only Windows may revoke another relay device")
            conn.execute(
                """
                UPDATE relay_devices
                SET revoked_at = ?, revocation_reason = ?
                WHERE device_id = ? AND revoked_at IS NULL
                """,
                (now, str(reason)[:500], target_device_id),
            )
            conn.execute(
                """
                INSERT INTO relay_revocations (
                    owner_id, actor_device_id, target_device_id, reason, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    target["owner_id"],
                    actor_device_id,
                    target_device_id,
                    str(reason)[:500],
                    now,
                ),
            )
        return {
            "device_id": target_device_id,
            "revoked_at": now,
            "reason": str(reason)[:500],
        }

    def upload_envelope(
        self,
        *,
        token: str,
        envelope: dict[str, Any],
    ) -> dict[str, Any]:
        identity = self.authenticate(token)
        normalized = self._validate_envelope(envelope)
        if normalized["sender_device_id"] != identity["device_id"]:
            raise PermissionError("relay envelope sender does not match bearer token")
        with self.store.connection() as conn:
            recipient = conn.execute(
                """
                SELECT owner_id, revoked_at
                FROM relay_devices
                WHERE device_id = ?
                """,
                (normalized["recipient_device_id"],),
            ).fetchone()
        if (
            recipient is None
            or recipient["revoked_at"] is not None
            or recipient["owner_id"] != identity["owner_id"]
        ):
            raise PermissionError("relay envelope recipient is unavailable")

        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        size = len(encoded.encode("utf-8"))
        if size > self.max_envelope_bytes:
            raise ValueError("relay envelope exceeds maximum size")
        now = _now()
        expires_at = now + timedelta(seconds=self.envelope_retention_seconds)
        try:
            with self.store.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO relay_envelopes (
                        message_id, owner_id, sender_device_id,
                        recipient_device_id, event_kind, sequence_number,
                        envelope_json, envelope_size, created_at, expires_at,
                        acknowledged_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        normalized["message_id"],
                        identity["owner_id"],
                        identity["device_id"],
                        normalized["recipient_device_id"],
                        normalized["event_kind"],
                        normalized["sequence_number"],
                        encoded,
                        size,
                        _timestamp(now),
                        _timestamp(expires_at),
                    ),
                )
        except sqlite3.IntegrityError:
            with self.store.connection() as conn:
                existing = conn.execute(
                    """
                    SELECT sender_device_id, recipient_device_id, envelope_json
                    FROM relay_envelopes
                    WHERE message_id = ?
                    """,
                    (normalized["message_id"],),
                ).fetchone()
            if (
                existing is None
                or existing["sender_device_id"] != identity["device_id"]
                or existing["recipient_device_id"] != normalized["recipient_device_id"]
                or existing["envelope_json"] != encoded
            ):
                raise PermissionError("relay message ID collision")
            return {
                "message_id": normalized["message_id"],
                "status": "duplicate",
            }
        try:
            self.apns_adapter.send_wake(
                recipient_device_id=normalized["recipient_device_id"],
                message_kind=normalized["event_kind"],
                routing_id=normalized["message_id"],
                collapse_id=(
                    f"project-q-{normalized['recipient_device_id']}-"
                    f"{normalized['event_kind']}"
                )[:64],
            )
        except Exception:
            pass
        return {
            "message_id": normalized["message_id"],
            "status": "stored",
            "expires_at": _timestamp(expires_at),
        }

    def pull_envelopes(
        self,
        *,
        token: str,
        limit: int = 200,
    ) -> dict[str, Any]:
        identity = self.authenticate(token)
        bounded_limit = max(1, min(int(limit), 500))
        now = _timestamp(_now())
        with self.store.connection() as conn:
            rows = conn.execute(
                """
                SELECT envelope_json, expires_at
                FROM relay_envelopes
                WHERE recipient_device_id = ?
                  AND acknowledged_at IS NULL
                  AND expires_at > ?
                ORDER BY created_at, message_id
                LIMIT ?
                """,
                (identity["device_id"], now, bounded_limit),
            ).fetchall()
        return {
            "items": [json.loads(row["envelope_json"]) for row in rows],
            "has_more": len(rows) == bounded_limit,
        }

    def acknowledge_envelope(
        self,
        *,
        token: str,
        message_id: str,
    ) -> dict[str, Any]:
        identity = self.authenticate(token)
        acknowledged_at = _timestamp(_now())
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT acknowledged_at
                FROM relay_envelopes
                WHERE message_id = ? AND recipient_device_id = ?
                """,
                (message_id, identity["device_id"]),
            ).fetchone()
            if row is None:
                raise KeyError(message_id)
            if row["acknowledged_at"]:
                acknowledged_at = str(row["acknowledged_at"])
            else:
                conn.execute(
                    """
                    UPDATE relay_envelopes
                    SET acknowledged_at = ?
                    WHERE message_id = ?
                      AND recipient_device_id = ?
                      AND acknowledged_at IS NULL
                    """,
                    (acknowledged_at, message_id, identity["device_id"]),
                )
        return {
            "message_id": message_id,
            "acknowledged_at": acknowledged_at,
        }

    def publish_presence(
        self,
        *,
        token: str,
        state: str,
        ttl_seconds: int = 120,
    ) -> dict[str, Any]:
        identity = self.authenticate(token)
        normalized_state = str(state).strip().lower()
        if normalized_state not in {"active", "idle", "background"}:
            raise ValueError("relay presence must be active, idle, or background")
        now = _now()
        expires_at = now + timedelta(seconds=max(15, min(int(ttl_seconds), 120)))
        with self.store.connection() as conn:
            conn.execute(
                """
                INSERT INTO relay_presence (
                    device_id, state, expires_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    state = excluded.state,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    identity["device_id"],
                    normalized_state,
                    _timestamp(expires_at),
                    _timestamp(now),
                ),
            )
        return {
            "device_id": identity["device_id"],
            "state": normalized_state,
            "expires_at": _timestamp(expires_at),
        }

    def list_presence(self, *, token: str) -> dict[str, Any]:
        identity = self.authenticate(token)
        now = _timestamp(_now())
        with self.store.connection() as conn:
            rows = conn.execute(
                """
                SELECT p.device_id, p.state, p.expires_at, p.updated_at
                FROM relay_presence AS p
                JOIN relay_devices AS d ON d.device_id = p.device_id
                WHERE d.owner_id = ?
                  AND d.revoked_at IS NULL
                  AND p.expires_at > ?
                ORDER BY p.updated_at DESC, p.device_id
                """,
                (identity["owner_id"], now),
            ).fetchall()
        return {
            "items": [
                {
                    "device_id": row["device_id"],
                    "state": row["state"],
                    "expires_at": row["expires_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
        }

    def register_apns_route(
        self,
        *,
        token: str,
        device_token: str,
        environment: str,
    ) -> dict[str, str]:
        if not self.apns_routes.available:
            raise RuntimeError("APNs route registration is not configured")
        identity = self.authenticate(token)
        if identity["role"] != "phone":
            raise PermissionError("only phone devices may register APNs routes")
        return self.apns_routes.set(
            device_id=identity["device_id"],
            device_token=device_token,
            environment=environment,
            updated_at=_timestamp(_now()),
        )

    @staticmethod
    def _validate_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(envelope, dict):
            raise ValueError("relay envelope must be an object")
        required = {
            "protocol_version",
            "algorithm",
            "message_id",
            "sender_device_id",
            "recipient_device_id",
            "event_kind",
            "sequence_number",
            "created_at",
            "nonce",
            "ciphertext",
            "tag",
            "content_type",
        }
        if set(envelope) != required:
            raise ValueError("relay accepts only the opaque envelope schema")
        normalized = dict(envelope)
        if int(normalized["protocol_version"]) != 2:
            raise ValueError("unsupported relay envelope protocol")
        if normalized["algorithm"] != "X25519-HKDF-SHA256-AES-256-GCM":
            raise ValueError("unsupported relay envelope algorithm")
        for key in (
            "message_id",
            "sender_device_id",
            "recipient_device_id",
            "event_kind",
            "created_at",
            "nonce",
            "ciphertext",
            "tag",
            "content_type",
        ):
            text = str(normalized[key]).strip()
            if not text or len(text) > 1_000_000:
                raise ValueError(f"invalid relay envelope {key}")
            normalized[key] = text
        if normalized["content_type"] != "application/json":
            raise ValueError("unsupported relay envelope content type")
        sequence = int(normalized["sequence_number"])
        if sequence < 1 or sequence > 9_223_372_036_854_775_807:
            raise ValueError("invalid relay envelope sequence")
        normalized["sequence_number"] = sequence
        normalized["protocol_version"] = 2
        return normalized
