from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from project_q.models import MemoryCreate, utc_now
from project_q.storage import Database


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


class CompanionService:
    def __init__(
        self,
        db: Database,
        vault_service,
        memory_service,
        audit_service,
        protocol_service=None,
    ) -> None:
        self.db = db
        self.vault_service = vault_service
        self.memory_service = memory_service
        self.audit_service = audit_service
        self.protocol_service = protocol_service

    def start_pairing_v2(self, *, device_name: str, platform: str = "ios") -> dict[str, Any]:
        if self.protocol_service is None:
            raise RuntimeError("companion protocol v2 is unavailable")
        return self.protocol_service.start_pairing(
            device_name=device_name,
            platform=platform,
        )

    def complete_pairing_v2(
        self,
        *,
        device_id: str,
        pairing_token: str,
        key_agreement_public_key: str,
        approval_public_key: str,
    ) -> dict[str, Any]:
        if self.protocol_service is None:
            raise RuntimeError("companion protocol v2 is unavailable")
        return self.protocol_service.complete_pairing(
            device_id=device_id,
            pairing_token=pairing_token,
            key_agreement_public_key=key_agreement_public_key,
            approval_public_key=approval_public_key,
        )

    def start_pairing(self, *, device_name: str, platform: str = "ios") -> dict[str, Any]:
        device_id = self.db.make_id("device")
        pairing_code = f"{secrets.randbelow(1_000_000):06d}"
        pairing_secret = _b64encode(secrets.token_bytes(32))
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO companion_devices (
                    id, device_name, platform, status, pairing_code_hash,
                    pairing_secret_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    str(device_name or "iPhone").strip()[:120],
                    str(platform or "ios").strip()[:40],
                    "pending",
                    self._hash_secret(pairing_code),
                    self._hash_secret(pairing_secret),
                    now,
                ),
            )
        self.audit_service.log(
            action_type="companion_pairing_start",
            action_tier=2,
            tool_name="companion_service",
            outcome="completed",
            approved_by_owner=True,
            input_sources=["owner"],
            metadata={"device_id": device_id, "platform": platform},
        )
        return {
            "device_id": device_id,
            "device_name": str(device_name or "iPhone").strip()[:120],
            "platform": str(platform or "ios").strip()[:40],
            "status": "pending",
            "pairing_code": pairing_code,
            "pairing_secret": pairing_secret,
            "created_at": now,
        }

    def complete_pairing(self, *, device_id: str, pairing_secret: str) -> dict[str, Any]:
        row = self._device_row(device_id)
        if row["status"] != "pending":
            raise PermissionError("device is not pending pairing")
        if not hmac.compare_digest(row["pairing_secret_hash"], self._hash_secret(pairing_secret)):
            raise PermissionError("invalid pairing secret")

        shared_key = _b64encode(secrets.token_bytes(32))
        secret_name = f"companion_shared_key_{device_id}"
        self.vault_service.set_secret(secret_name, shared_key, f"Project Q companion key for {row['device_name']}")
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE companion_devices
                SET status = 'paired',
                    pairing_code_hash = '',
                    pairing_secret_hash = '',
                    shared_key_secret_name = ?,
                    paired_at = ?,
                    last_seen_at = ?
                WHERE id = ?
                """,
                (secret_name, now, now, device_id),
            )
        self.audit_service.log(
            action_type="companion_pairing_complete",
            action_tier=2,
            tool_name="companion_service",
            outcome="completed",
            approved_by_owner=True,
            input_sources=["owner_device"],
            metadata={"device_id": device_id},
        )
        return {
            "device_id": device_id,
            "device_name": row["device_name"],
            "platform": row["platform"],
            "status": "paired",
            "shared_key": shared_key,
            "paired_at": now,
        }

    def list_devices(self) -> list[dict[str, Any]]:
        self._expire_presence()
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, device_name, platform, status, protocol_version,
                       presence_state, presence_expires_at,
                       created_at, paired_at, last_seen_at, revoked_at
                FROM companion_devices
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "device_name": row["device_name"],
                "platform": row["platform"],
                "status": row["status"],
                "protocol_version": int(row["protocol_version"]),
                "presence_state": row["presence_state"],
                "presence_expires_at": row["presence_expires_at"] or "",
                "created_at": row["created_at"],
                "paired_at": row["paired_at"] or "",
                "last_seen_at": row["last_seen_at"] or "",
                "revoked_at": row["revoked_at"] or "",
            }
            for row in rows
        ]

    def publish_presence(
        self,
        device_id: str,
        *,
        state: str,
        ttl_seconds: int = 120,
    ) -> dict[str, Any]:
        normalized_state = str(state).strip().lower()
        if normalized_state not in {"active", "idle", "background"}:
            raise ValueError("presence state must be active, idle, or background")
        row = self._device_row(device_id)
        if (
            row["status"] != "paired"
            or int(row["protocol_version"]) != 2
            or bool(row["legacy_repair_required"])
        ):
            raise PermissionError("companion device is not active")
        now = datetime.now(UTC).replace(microsecond=0)
        expires_at = now + timedelta(seconds=max(15, min(int(ttl_seconds), 120)))
        now_text = now.isoformat().replace("+00:00", "Z")
        expires_text = expires_at.isoformat().replace("+00:00", "Z")
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE companion_devices
                SET presence_state = ?, presence_expires_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (normalized_state, expires_text, now_text, device_id),
            )
        return {
            "device_id": device_id,
            "state": normalized_state,
            "expires_at": expires_text,
            "advisory": True,
        }

    def get_presence(self, device_id: str) -> dict[str, Any]:
        self._expire_presence(device_id=device_id)
        row = self._device_row(device_id)
        return {
            "device_id": device_id,
            "state": row["presence_state"],
            "expires_at": row["presence_expires_at"] or "",
            "advisory": True,
        }

    def revoke_device(self, device_id: str, *, reason: str = "", source: str = "dashboard") -> dict[str, Any]:
        row = self._device_row(device_id)
        now = utc_now()
        secret_names = {
            str(row[key])
            for key in (
                "shared_key_secret_name",
                "send_key_secret_name",
                "receive_key_secret_name",
                "request_key_secret_name",
            )
            if key in row.keys() and row[key]
        }
        for secret_name in secret_names:
            self.vault_service.delete_secret(secret_name)
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE companion_devices
                SET status = 'revoked',
                    shared_key_secret_name = '',
                    send_key_secret_name = '',
                    receive_key_secret_name = '',
                    request_key_secret_name = '',
                    presence_state = 'offline',
                    presence_expires_at = NULL,
                    revoked_at = ?
                WHERE id = ?
                """,
                (now, device_id),
            )
        self.audit_service.log(
            action_type="companion_device_revoke",
            action_tier=3,
            tool_name="companion_service",
            outcome="completed",
            approved_by_owner=True,
            input_sources=[source],
            metadata={"device_id": device_id, "reason": reason, "source": source},
        )
        revoked = dict(self._device_public(device_id))
        return revoked

    def sign_request(
        self,
        *,
        device_id: str,
        shared_key: str,
        method: str,
        path: str,
        timestamp: str,
        body: bytes,
    ) -> str:
        del device_id
        return self._request_signature(_b64decode(shared_key), method=method, path=path, timestamp=timestamp, body=body)

    def verify_request(
        self,
        *,
        device_id: str,
        method: str,
        path: str,
        timestamp: str,
        body: bytes,
        signature: str,
        max_age_seconds: int | None = 300,
    ) -> dict[str, Any]:
        row = self._device_row(device_id)
        if row["status"] != "paired":
            raise PermissionError("device is not paired")
        if max_age_seconds is not None:
            self._verify_timestamp(timestamp, max_age_seconds)
        expected = self._request_signature(
            self._device_key(row),
            method=method,
            path=path,
            timestamp=timestamp,
            body=body,
        )
        if not hmac.compare_digest(expected, signature):
            raise PermissionError("invalid companion request signature")
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute("UPDATE companion_devices SET last_seen_at = ? WHERE id = ?", (now, device_id))
        return {
            "device_id": row["id"],
            "device_name": row["device_name"],
            "platform": row["platform"],
            "status": row["status"],
        }

    def seal_envelope(self, device_id: str, payload: dict[str, Any], *, kind: str) -> dict[str, Any]:
        row = self._device_row(device_id)
        key = self._device_key(row)
        nonce = secrets.token_bytes(16)
        plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ciphertext = self._xor_stream(key, nonce, plaintext)
        kind_text = str(kind or "message").strip()[:80]
        aad = self._envelope_aad(device_id, kind_text, nonce)
        tag = hmac.new(key, aad + ciphertext, hashlib.sha256).digest()
        return {
            "device_id": device_id,
            "kind": kind_text,
            "nonce": _b64encode(nonce),
            "ciphertext": _b64encode(ciphertext),
            "tag": _b64encode(tag),
            "content_type": "application/json",
            "algorithm": "PROJECTQ-HMAC-SHA256-STREAM-V1",
        }

    def open_envelope(self, device_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
        row = self._device_row(device_id)
        key = self._device_key(row)
        kind = str(envelope.get("kind", "message"))
        nonce = _b64decode(str(envelope["nonce"]))
        ciphertext = _b64decode(str(envelope["ciphertext"]))
        received_tag = _b64decode(str(envelope["tag"]))
        expected_tag = hmac.new(key, self._envelope_aad(device_id, kind, nonce) + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(received_tag, expected_tag):
            raise PermissionError("invalid companion envelope tag")
        plaintext = self._xor_stream(key, nonce, ciphertext)
        payload = json.loads(plaintext.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("companion envelope must contain a JSON object")
        return payload

    def ingest_quick_capture(self, device_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
        payload = self.open_envelope(device_id, envelope)
        if payload.get("kind") != "quick_capture":
            raise ValueError("quick capture envelope requires kind=quick_capture")
        text = str(payload.get("text", "")).strip()
        if not text:
            raise ValueError("quick capture text is required")
        device = self._device_row(device_id)
        memory = self.memory_service.create(
            MemoryCreate(
                text=text,
                kind="episodic",
                source="iphone_companion",
                confidence=0.75,
                owner_confirmed=False,
                tags=["iphone", "quick_capture", "external_device"],
                metadata={
                    "device_id": device_id,
                    "device_name": device["device_name"],
                    "trust_zone": "zone_2_owner_device",
                    "encrypted_envelope": True,
                },
            )
        )
        envelope_id = self._store_envelope(device_id, "inbound", envelope, metadata={"created_memory_id": memory["id"]})
        self.audit_service.log(
            action_type="companion_quick_capture",
            action_tier=1,
            tool_name="companion_service",
            outcome="completed",
            input_sources=["iphone_companion"],
            metadata={"device_id": device_id, "envelope_id": envelope_id, "memory_id": memory["id"]},
        )
        return {"created_memory": memory, "envelope_id": envelope_id}

    def _store_envelope(
        self,
        device_id: str,
        direction: str,
        envelope: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        envelope_id = self.db.make_id("env")
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO companion_envelopes (
                    id, device_id, direction, kind, nonce, ciphertext, tag,
                    metadata_json, created_at, opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope_id,
                    device_id,
                    direction,
                    str(envelope.get("kind", "message")),
                    str(envelope["nonce"]),
                    str(envelope["ciphertext"]),
                    str(envelope["tag"]),
                    self.db.dumps(metadata or {}),
                    now,
                    now if direction == "inbound" else None,
                ),
            )
        return envelope_id

    def _device_row(self, device_id: str):
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM companion_devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            raise KeyError(device_id)
        return row

    def _device_public(self, device_id: str) -> dict[str, Any]:
        self._expire_presence(device_id=device_id)
        row = self._device_row(device_id)
        return {
            "id": row["id"],
            "device_name": row["device_name"],
            "platform": row["platform"],
            "status": row["status"],
            "protocol_version": int(row["protocol_version"]),
            "presence_state": row["presence_state"],
            "presence_expires_at": row["presence_expires_at"] or "",
            "created_at": row["created_at"],
            "paired_at": row["paired_at"] or "",
            "last_seen_at": row["last_seen_at"] or "",
            "revoked_at": row["revoked_at"] or "",
        }

    def _expire_presence(self, *, device_id: str | None = None) -> None:
        now = utc_now()
        query = (
            """
            UPDATE companion_devices
            SET presence_state = 'offline', presence_expires_at = NULL
            WHERE presence_expires_at IS NOT NULL
              AND presence_expires_at <= ?
            """
        )
        parameters: list[Any] = [now]
        if device_id is not None:
            query += " AND id = ?"
            parameters.append(device_id)
        with self.db.connection() as conn:
            conn.execute(query, tuple(parameters))

    def _device_key(self, row) -> bytes:
        secret_name = row["shared_key_secret_name"]
        if not secret_name:
            raise PermissionError("device does not have a shared key")
        return _b64decode(self.vault_service.get_secret(secret_name))

    @staticmethod
    def _hash_secret(value: str) -> str:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _request_signature(key: bytes, *, method: str, path: str, timestamp: str, body: bytes) -> str:
        body_hash = hashlib.sha256(body or b"").hexdigest()
        signing_text = f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}".encode("utf-8")
        return _b64encode(hmac.new(key, signing_text, hashlib.sha256).digest())

    @staticmethod
    def _verify_timestamp(timestamp: str, max_age_seconds: int) -> None:
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PermissionError("invalid companion timestamp") from exc
        age = abs((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())
        if age > max_age_seconds:
            raise PermissionError("stale companion request timestamp")

    @staticmethod
    def _xor_stream(key: bytes, nonce: bytes, payload: bytes) -> bytes:
        output = bytearray()
        counter = 0
        while len(output) < len(payload):
            block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
            output.extend(block)
            counter += 1
        return bytes(left ^ right for left, right in zip(payload, output))

    @staticmethod
    def _envelope_aad(device_id: str, kind: str, nonce: bytes) -> bytes:
        return f"{device_id}\n{kind}\n{_b64encode(nonce)}\n".encode("utf-8")
