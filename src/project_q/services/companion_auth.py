from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
from datetime import UTC, datetime
from typing import Any

from project_q.models import utc_now


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class CompanionAuthService:
    def __init__(self, db, protocol_service, *, max_age_seconds: int = 300) -> None:
        self.db = db
        self.protocol_service = protocol_service
        self.max_age_seconds = max_age_seconds

    def sign_request(
        self,
        *,
        request_key: bytes,
        device_id: str,
        method: str,
        path: str,
        timestamp: str,
        request_nonce: str,
        sequence_number: int,
        body: bytes,
    ) -> str:
        if not isinstance(request_key, bytes) or len(request_key) != 32:
            raise ValueError("companion request key must be 32 bytes")
        signing_bytes = self._signing_bytes(
            device_id=device_id,
            method=method,
            path=path,
            timestamp=timestamp,
            request_nonce=request_nonce,
            sequence_number=sequence_number,
            body=body,
        )
        return _b64encode(hmac.new(request_key, signing_bytes, hashlib.sha256).digest())

    def verify_request(
        self,
        *,
        device_id: str,
        method: str,
        path: str,
        timestamp: str,
        request_nonce: str,
        sequence_number: int,
        body: bytes,
        signature: str,
        max_age_seconds: int | None = None,
    ) -> dict[str, Any]:
        self._verify_timestamp(
            timestamp,
            self.max_age_seconds if max_age_seconds is None else max_age_seconds,
        )
        request_key = self.protocol_service.device_keys(device_id)["request_key"]
        expected = self.sign_request(
            request_key=request_key,
            device_id=device_id,
            method=method,
            path=path,
            timestamp=timestamp,
            request_nonce=request_nonce,
            sequence_number=sequence_number,
            body=body,
        )
        if not hmac.compare_digest(expected, str(signature)):
            raise PermissionError("invalid companion request signature")

        now = utc_now()
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT id, device_name, platform, status, protocol_version,
                           receive_sequence, legacy_repair_required
                    FROM companion_devices
                    WHERE id = ?
                    """,
                    (device_id,),
                ).fetchone()
                if row is None:
                    raise PermissionError("unknown companion device")
                if (
                    row["status"] != "paired"
                    or int(row["protocol_version"]) != 2
                    or bool(row["legacy_repair_required"])
                ):
                    raise PermissionError("companion device is not active")
                if sequence_number <= int(row["receive_sequence"]):
                    raise PermissionError("companion request sequence was already used")
                conn.execute(
                    """
                    INSERT INTO companion_request_nonces (
                        id, device_id, request_nonce, sequence_number,
                        request_timestamp, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.db.make_id("nonce"),
                        device_id,
                        request_nonce,
                        sequence_number,
                        timestamp,
                        now,
                    ),
                )
                cursor = conn.execute(
                    """
                    UPDATE companion_devices
                    SET receive_sequence = ?, last_seen_at = ?
                    WHERE id = ? AND receive_sequence < ?
                    """,
                    (sequence_number, now, device_id, sequence_number),
                )
                if cursor.rowcount != 1:
                    raise PermissionError("companion request sequence conflict")
        except sqlite3.IntegrityError as exc:
            raise PermissionError("companion request replay detected") from exc

        return {
            "device_id": row["id"],
            "device_name": row["device_name"],
            "platform": row["platform"],
            "status": row["status"],
            "sequence_number": sequence_number,
        }

    def _signing_bytes(
        self,
        *,
        device_id: str,
        method: str,
        path: str,
        timestamp: str,
        request_nonce: str,
        sequence_number: int,
        body: bytes,
    ) -> bytes:
        device = self._required_text(device_id, "device ID", 200)
        request_path = self._required_text(path, "request path", 2000)
        nonce = self._required_text(request_nonce, "request nonce", 200)
        if len(nonce) < 16:
            raise ValueError("companion request nonce is too short")
        sequence = int(sequence_number)
        if sequence < 1 or sequence > 9_223_372_036_854_775_807:
            raise ValueError("invalid companion request sequence")
        body_hash = hashlib.sha256(body or b"").hexdigest()
        return (
            "PROJECTQ-COMPANION-REQUEST-V2\n"
            f"{device}\n"
            f"{str(method).upper()}\n"
            f"{request_path}\n"
            f"{timestamp}\n"
            f"{nonce}\n"
            f"{sequence}\n"
            f"{body_hash}"
        ).encode("utf-8")

    @staticmethod
    def _verify_timestamp(timestamp: str, max_age_seconds: int | None) -> None:
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError as exc:
            raise PermissionError("invalid companion request timestamp") from exc
        if parsed.tzinfo is None:
            raise PermissionError("companion request timestamp must include a timezone")
        if max_age_seconds is None:
            return
        age = abs((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())
        if age > max_age_seconds:
            raise PermissionError("stale companion request timestamp")

    @staticmethod
    def _required_text(value: Any, label: str, maximum: int) -> str:
        text = str(value).strip()
        if not text or len(text) > maximum:
            raise ValueError(f"invalid {label}")
        return text
