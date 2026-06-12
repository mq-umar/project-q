from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from project_q.models import utc_now
from project_q.services.companion_crypto import (
    PROTOCOL_VERSION,
    CompanionCryptoService,
)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    text = str(value)
    return base64.b64decode(
        (text + ("=" * (-len(text) % 4))).encode("ascii"),
        altchars=b"-_",
        validate=True,
    )


class CompanionProtocolService:
    def __init__(self, db, vault_service, audit_service, crypto_service=None) -> None:
        self.db = db
        self.vault_service = vault_service
        self.audit_service = audit_service
        self.crypto = crypto_service or CompanionCryptoService()
        self._invalidate_legacy_devices()

    def start_pairing(
        self,
        *,
        device_name: str,
        platform: str = "ios",
        ttl_seconds: int = 600,
    ) -> dict[str, Any]:
        device_id = self.db.make_id("device")
        pairing_id = self.db.make_id("pairing")
        pairing_token_bytes = secrets.token_bytes(32)
        pairing_token = _b64encode(pairing_token_bytes)
        key_pair = self.crypto.generate_key_pair()
        private_secret_name = f"companion_pairing_private_{pairing_id}"
        self.vault_service.set_secret(
            private_secret_name,
            self.crypto.serialize_private_key(key_pair.private_key),
            "Temporary Project Q companion pairing key",
        )
        now = datetime.now(UTC).replace(microsecond=0)
        expires_at = now + timedelta(seconds=max(60, min(int(ttl_seconds), 600)))
        windows_device_id = self._windows_device_id()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO companion_devices (
                    id, device_name, platform, status, protocol_version,
                    pairing_code_hash, pairing_secret_hash,
                    key_agreement_public_key, approval_public_key,
                    send_key_secret_name, receive_key_secret_name,
                    request_key_secret_name, receive_sequence, send_sequence,
                    presence_state, legacy_repair_required, created_at
                ) VALUES (?, ?, ?, 'pending', ?, '', '', '', '', '', '', '', 0, 0, 'offline', 0, ?)
                """,
                (
                    device_id,
                    str(device_name or "iPhone").strip()[:120],
                    str(platform or "ios").strip()[:40],
                    PROTOCOL_VERSION,
                    now.isoformat().replace("+00:00", "Z"),
                ),
            )
            conn.execute(
                """
                INSERT INTO companion_pairing_sessions (
                    id, device_id, pairing_token_hash,
                    windows_private_key_secret_name, windows_public_key,
                    expires_at, used_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    pairing_id,
                    device_id,
                    hashlib.sha256(pairing_token_bytes).hexdigest(),
                    private_secret_name,
                    key_pair.public_key,
                    expires_at.isoformat().replace("+00:00", "Z"),
                    now.isoformat().replace("+00:00", "Z"),
                ),
            )
        self.audit_service.log(
            action_type="companion_pairing_v2_start",
            action_tier=2,
            tool_name="companion_protocol",
            outcome="completed",
            approved_by_owner=True,
            input_sources=["owner"],
            metadata={
                "device_id": device_id,
                "platform": platform,
                "protocol_version": PROTOCOL_VERSION,
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            },
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "device_id": device_id,
            "windows_device_id": windows_device_id,
            "device_name": str(device_name or "iPhone").strip()[:120],
            "platform": str(platform or "ios").strip()[:40],
            "status": "pending",
            "windows_key_agreement_public_key": key_pair.public_key,
            "pairing_token": pairing_token,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }

    def complete_pairing(
        self,
        *,
        device_id: str,
        pairing_token: str,
        key_agreement_public_key: str,
        approval_public_key: str,
    ) -> dict[str, Any]:
        token_bytes = self._decode_pairing_token(pairing_token)
        self._validate_approval_public_key(approval_public_key)
        now = datetime.now(UTC).replace(microsecond=0)
        with self.db.connection() as conn:
            device = conn.execute(
                "SELECT * FROM companion_devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            pairing = conn.execute(
                """
                SELECT *
                FROM companion_pairing_sessions
                WHERE device_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (device_id,),
            ).fetchone()
        if device is None or pairing is None:
            raise PermissionError("unknown companion pairing")
        if device["status"] != "pending" or pairing["used_at"]:
            raise PermissionError("companion pairing is no longer pending")
        expires_at = datetime.fromisoformat(str(pairing["expires_at"]).replace("Z", "+00:00"))
        if now > expires_at.astimezone(UTC):
            raise PermissionError("companion pairing has expired")
        expected_hash = str(pairing["pairing_token_hash"])
        received_hash = hashlib.sha256(token_bytes).hexdigest()
        if not hmac.compare_digest(expected_hash, received_hash):
            raise PermissionError("invalid companion pairing token")

        private_secret_name = str(pairing["windows_private_key_secret_name"])
        private_key = self.crypto.load_private_key(
            self.vault_service.get_secret(private_secret_name)
        )
        windows_device_id = self._windows_device_id()
        session_keys = self.crypto.derive_session_keys(
            private_key=private_key,
            peer_public_key=key_agreement_public_key,
            pairing_token=token_bytes,
            local_device_id=windows_device_id,
            remote_device_id=device_id,
        )
        key_names = {
            "send": f"companion_v2_send_{device_id}",
            "receive": f"companion_v2_receive_{device_id}",
            "request": f"companion_v2_request_{device_id}",
        }
        self.vault_service.set_secret(
            key_names["send"],
            _b64encode(session_keys.send_key),
            f"Project Q v2 send key for {device['device_name']}",
        )
        self.vault_service.set_secret(
            key_names["receive"],
            _b64encode(session_keys.receive_key),
            f"Project Q v2 receive key for {device['device_name']}",
        )
        self.vault_service.set_secret(
            key_names["request"],
            _b64encode(session_keys.request_key),
            f"Project Q v2 request key for {device['device_name']}",
        )
        paired_at = now.isoformat().replace("+00:00", "Z")
        try:
            with self.db.connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE companion_pairing_sessions
                    SET used_at = ?
                    WHERE id = ? AND used_at IS NULL
                    """,
                    (paired_at, pairing["id"]),
                )
                if cursor.rowcount != 1:
                    raise PermissionError("companion pairing was already used")
                conn.execute(
                    """
                    UPDATE companion_devices
                    SET status = 'paired',
                        protocol_version = ?,
                        key_agreement_public_key = ?,
                        approval_public_key = ?,
                        send_key_secret_name = ?,
                        receive_key_secret_name = ?,
                        request_key_secret_name = ?,
                        shared_key_secret_name = '',
                        paired_at = ?,
                        last_seen_at = ?,
                        legacy_repair_required = 0
                    WHERE id = ? AND status = 'pending'
                    """,
                    (
                        PROTOCOL_VERSION,
                        key_agreement_public_key,
                        approval_public_key,
                        key_names["send"],
                        key_names["receive"],
                        key_names["request"],
                        paired_at,
                        paired_at,
                        device_id,
                    ),
                )
        except Exception:
            for secret_name in key_names.values():
                self.vault_service.delete_secret(secret_name)
            raise
        finally:
            self.vault_service.delete_secret(private_secret_name)

        self.audit_service.log(
            action_type="companion_pairing_v2_complete",
            action_tier=2,
            tool_name="companion_protocol",
            outcome="completed",
            approved_by_owner=True,
            input_sources=["owner_device"],
            metadata={
                "device_id": device_id,
                "protocol_version": PROTOCOL_VERSION,
            },
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "device_id": device_id,
            "windows_device_id": windows_device_id,
            "device_name": device["device_name"],
            "platform": device["platform"],
            "status": "paired",
            "paired_at": paired_at,
        }

    def device_keys(self, device_id: str) -> dict[str, bytes]:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM companion_devices WHERE id = ?",
                (device_id,),
            ).fetchone()
        if row is None or row["status"] != "paired" or int(row["protocol_version"]) != 2:
            raise PermissionError("companion device is not paired with protocol v2")
        return {
            "send_key": _b64decode(
                self.vault_service.get_secret(str(row["send_key_secret_name"]))
            ),
            "receive_key": _b64decode(
                self.vault_service.get_secret(str(row["receive_key_secret_name"]))
            ),
            "request_key": _b64decode(
                self.vault_service.get_secret(str(row["request_key_secret_name"]))
            ),
        }

    def _invalidate_legacy_devices(self) -> None:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, shared_key_secret_name
                FROM companion_devices
                WHERE status = 'paired'
                  AND (
                      protocol_version < ?
                      OR legacy_repair_required = 1
                  )
                """,
                (PROTOCOL_VERSION,),
            ).fetchall()
        for row in rows:
            secret_name = str(row["shared_key_secret_name"] or "")
            if secret_name:
                self.vault_service.delete_secret(secret_name)
            now = utc_now()
            with self.db.connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE companion_devices
                    SET status = 'repair_required',
                        shared_key_secret_name = '',
                        pairing_code_hash = '',
                        pairing_secret_hash = '',
                        presence_state = 'offline',
                        presence_expires_at = NULL,
                        legacy_repair_required = 1
                    WHERE id = ? AND status = 'paired'
                    """,
                    (row["id"],),
                )
            if cursor.rowcount != 1:
                continue
            self.audit_service.log(
                action_type="companion_legacy_invalidate",
                action_tier=3,
                tool_name="companion_protocol",
                outcome="repair_required",
                approved_by_owner=False,
                input_sources=["system"],
                metadata={
                    "device_id": row["id"],
                    "invalidated_at": now,
                    "required_protocol_version": PROTOCOL_VERSION,
                },
            )

    def _windows_device_id(self) -> str:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT value_json FROM settings WHERE key = 'companion_windows_device_id'"
            ).fetchone()
            if row is not None:
                return str(json.loads(row["value_json"]))
            device_id = self.db.make_id("windows")
            conn.execute(
                """
                INSERT INTO settings (key, value_json, updated_at)
                VALUES ('companion_windows_device_id', ?, ?)
                """,
                (json.dumps(device_id), utc_now()),
            )
            return device_id

    def windows_device_id(self) -> str:
        return self._windows_device_id()

    @staticmethod
    def _decode_pairing_token(value: str) -> bytes:
        try:
            token = _b64decode(value)
        except Exception as exc:
            raise PermissionError("invalid companion pairing token") from exc
        if len(token) != 32:
            raise PermissionError("invalid companion pairing token")
        return token

    @staticmethod
    def _validate_approval_public_key(value: str) -> None:
        try:
            raw = _b64decode(value)
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        except Exception as exc:
            raise ValueError("invalid P-256 approval public key") from exc
