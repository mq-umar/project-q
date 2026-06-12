from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from project_q.models import utc_now
from project_q.services.sync import SyncEventService


_PAYLOAD_KEY_NAME = "companion_approval_payload_key_v1"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    text = str(value)
    return base64.b64decode(
        (text + ("=" * (-len(text) % 4))).encode("ascii"),
        altchars=b"-_",
        validate=True,
    )


class ApprovalService:
    def __init__(
        self,
        db,
        vault_service,
        tool_registry,
        policy_service,
        audit_service,
        sync_service,
        *,
        max_signature_age_seconds: int = 300,
    ) -> None:
        self.db = db
        self.vault_service = vault_service
        self.tool_registry = tool_registry
        self.policy_service = policy_service
        self.audit_service = audit_service
        self.sync_service = sync_service
        self.max_signature_age_seconds = max_signature_age_seconds

    def create_request(
        self,
        *,
        tool_id: str,
        action_tier: int,
        payload: dict[str, Any],
        summary: str,
        session_id: str = "",
        originating_goal: str = "",
        input_sources: list[str] | None = None,
        model: str = "",
        plan_id: str = "",
        ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        tool = self.tool_registry.get(tool_id)
        tier = int(action_tier)
        if tier not in {2, 3} or int(tool.definition.tier) != tier:
            raise ValueError("approval requests are limited to matching Tier 2 or Tier 3 tools")
        sources = [str(source) for source in (input_sources or ["owner"])]
        policy = self.policy_service.authorize_tool(
            tier=tier,
            owner_approved=False,
            input_sources=sources,
            tool_id=tool_id,
        )
        if policy.allowed or policy.reason != f"tier {tier} requires owner approval":
            raise PermissionError(
                "action is not blocked solely for missing owner approval"
            )
        if not isinstance(payload, dict):
            raise ValueError("approval payload must be an object")

        action_id = self.db.make_id("action")
        canonical_payload = self._canonical_json(payload)
        payload_digest = hashlib.sha256(canonical_payload).hexdigest()
        encrypted_payload = self._encrypt_payload(
            action_id=action_id,
            canonical_payload=canonical_payload,
        )
        now = datetime.now(UTC).replace(microsecond=0)
        expires_at = now + timedelta(seconds=max(60, min(int(ttl_seconds), 3600)))
        created_at = now.isoformat().replace("+00:00", "Z")
        expires_text = expires_at.isoformat().replace("+00:00", "Z")
        redacted_preview = SyncEventService._redact(payload)
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO action_requests (
                    id, session_id, action_tier, tool_name, summary,
                    redacted_preview_json, frozen_payload_json, payload_digest,
                    originating_goal, input_sources_json, model, plan_id,
                    status, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    action_id,
                    str(session_id)[:200],
                    tier,
                    tool_id,
                    str(summary or tool.definition.name).strip()[:1000],
                    self.db.dumps(redacted_preview),
                    self.db.dumps(encrypted_payload),
                    payload_digest,
                    str(originating_goal)[:4000],
                    self.db.dumps(sources),
                    str(model)[:300],
                    str(plan_id)[:200],
                    expires_text,
                    created_at,
                    created_at,
                ),
            )
        request = self.get(action_id)
        self.audit_service.log(
            action_type="approval_request_create",
            action_tier=tier,
            tool_name=tool_id,
            outcome="pending",
            input_sources=sources,
            metadata={
                "action_request_id": action_id,
                "payload_digest": payload_digest,
                "expires_at": expires_text,
            },
            model=str(model or "local-mvp"),
        )
        self.sync_service.append(
            resource_type="approval",
            resource_id=action_id,
            operation="created",
            payload=request,
        )
        return request

    def list_pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self._expire_pending()
        bounded_limit = max(1, min(int(limit), 500))
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM action_requests
                WHERE status = 'pending'
                ORDER BY created_at, id
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get(self, action_request_id: str) -> dict[str, Any]:
        self._expire_pending(action_request_id=action_request_id)
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM action_requests WHERE id = ?",
                (action_request_id,),
            ).fetchone()
        if row is None:
            raise KeyError(action_request_id)
        return self._row_to_dict(row)

    def decision_message(
        self,
        *,
        action_request: dict[str, Any],
        decision: str,
        device_id: str,
        timestamp: str,
    ) -> bytes:
        normalized_decision = self._decision(decision)
        return (
            "PROJECTQ-APPROVAL-V2\n"
            f"{action_request['id']}\n"
            f"{normalized_decision}\n"
            f"{int(action_request['action_tier'])}\n"
            f"{action_request['payload_digest']}\n"
            f"{action_request['expires_at']}\n"
            f"{str(device_id).strip()}\n"
            f"{timestamp}"
        ).encode("utf-8")

    def decide(
        self,
        *,
        action_request_id: str,
        device_id: str,
        decision: str,
        timestamp: str,
        signature: str,
        biometric_backed: bool,
    ) -> dict[str, Any]:
        normalized_decision = self._decision(decision)
        self._verify_timestamp(timestamp)
        with self.db.connection() as conn:
            request_row = conn.execute(
                "SELECT * FROM action_requests WHERE id = ?",
                (action_request_id,),
            ).fetchone()
            device_row = conn.execute(
                """
                SELECT id, status, protocol_version, legacy_repair_required,
                       approval_public_key
                FROM companion_devices
                WHERE id = ?
                """,
                (device_id,),
            ).fetchone()
        if request_row is None:
            raise KeyError(action_request_id)
        self._require_active_device(device_row)
        request = self._row_to_dict(request_row)
        if request["status"] != "pending":
            raise PermissionError("approval request is no longer pending")
        if self._is_expired(request["expires_at"]):
            self._expire_pending(action_request_id=action_request_id)
            raise PermissionError("approval request has expired")
        if (
            normalized_decision == "approve"
            and int(request["action_tier"]) == 3
            and not biometric_backed
        ):
            raise PermissionError("Tier 3 approval requires biometric backing")

        message = self.decision_message(
            action_request=request,
            decision=normalized_decision,
            device_id=device_id,
            timestamp=timestamp,
        )
        self._verify_signature(
            public_key=str(device_row["approval_public_key"]),
            signature=signature,
            message=message,
        )
        decided_at = utc_now()
        new_status = "approved" if normalized_decision == "approve" else "rejected"
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT status, expires_at FROM action_requests WHERE id = ?",
                    (action_request_id,),
                ).fetchone()
                if current is None or current["status"] != "pending":
                    raise PermissionError("approval request is no longer pending")
                if self._is_expired(str(current["expires_at"])):
                    conn.execute(
                        """
                        UPDATE action_requests
                        SET status = 'expired', updated_at = ?
                        WHERE id = ? AND status = 'pending'
                        """,
                        (decided_at, action_request_id),
                    )
                    raise PermissionError("approval request has expired")
                conn.execute(
                    """
                    INSERT INTO action_decisions (
                        id, action_request_id, device_id, decision,
                        biometric_backed, signature, payload_digest, decided_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.db.make_id("decision"),
                        action_request_id,
                        device_id,
                        normalized_decision,
                        1 if biometric_backed else 0,
                        str(signature),
                        request["payload_digest"],
                        decided_at,
                    ),
                )
                cursor = conn.execute(
                    """
                    UPDATE action_requests
                    SET status = ?, updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (new_status, decided_at, action_request_id),
                )
                if cursor.rowcount != 1:
                    raise PermissionError("approval request decision conflict")
        except sqlite3.IntegrityError as exc:
            raise PermissionError("approval request was already decided") from exc

        updated = self.get(action_request_id)
        self.audit_service.log(
            action_type="approval_decision",
            action_tier=int(request["action_tier"]),
            tool_name=request["tool_name"],
            outcome=new_status,
            approved_by_owner=normalized_decision == "approve",
            input_sources=["iphone_companion"],
            metadata={
                "action_request_id": action_request_id,
                "device_id": device_id,
                "biometric_backed": bool(biometric_backed),
                "payload_digest": request["payload_digest"],
            },
        )
        self.sync_service.append(
            resource_type="approval",
            resource_id=action_request_id,
            operation=new_status,
            payload=updated,
        )
        return updated

    def execute_approved(self, action_request_id: str) -> dict[str, Any]:
        claimed_at = utc_now()
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM action_requests WHERE id = ?",
                (action_request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(action_request_id)
            if row["status"] in {"completed", "failed", "rejected", "expired", "executing"}:
                return self._row_to_dict(row)
            if row["status"] != "approved":
                raise PermissionError("approval request has not been approved")
            cursor = conn.execute(
                """
                UPDATE action_requests
                SET status = 'executing', updated_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (claimed_at, action_request_id),
            )
            if cursor.rowcount != 1:
                current = conn.execute(
                    "SELECT * FROM action_requests WHERE id = ?",
                    (action_request_id,),
                ).fetchone()
                return self._row_to_dict(current)
            claimed = dict(row)

        try:
            payload = self._decrypt_payload(
                action_id=action_request_id,
                encrypted_payload=self.db.loads(claimed["frozen_payload_json"]),
            )
            digest = hashlib.sha256(self._canonical_json(payload)).hexdigest()
        except Exception:
            return self._finish_execution(
                claimed,
                status="failed",
                outcome="payload_integrity_failed",
                error="frozen approval payload could not be authenticated",
            )
        if not secrets.compare_digest(digest, str(claimed["payload_digest"])):
            return self._finish_execution(
                claimed,
                status="failed",
                outcome="payload_integrity_failed",
                error="frozen approval payload digest mismatch",
            )

        sources = list(self.db.loads(claimed["input_sources_json"]))
        execution_sources = [*sources, "owner_session", "iphone_companion"]
        policy = self.policy_service.authorize_tool(
            tier=int(claimed["action_tier"]),
            owner_approved=True,
            input_sources=execution_sources,
            tool_id=str(claimed["tool_name"]),
        )
        if not policy.allowed:
            return self._finish_execution(
                claimed,
                status="failed",
                outcome="policy_blocked",
                error=policy.reason,
            )

        try:
            result = self.tool_registry.get(str(claimed["tool_name"])).execute(payload)
        except Exception as exc:
            return self._finish_execution(
                claimed,
                status="failed",
                outcome="execution_failed",
                error=str(exc)[:2000],
            )
        return self._finish_execution(
            claimed,
            status="completed",
            outcome="completed",
            result=result,
        )

    def _finish_execution(
        self,
        claimed: dict[str, Any],
        *,
        status: str,
        outcome: str,
        error: str = "",
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        completed_at = utc_now()
        result_summary = SyncEventService._redact(result or {})
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE action_requests
                SET status = ?, updated_at = ?, executed_at = ?,
                    execution_outcome = ?, execution_error = ?
                WHERE id = ? AND status = 'executing'
                """,
                (
                    status,
                    completed_at,
                    completed_at,
                    outcome,
                    str(error)[:2000],
                    claimed["id"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("approval execution state changed unexpectedly")
        updated = self.get(str(claimed["id"]))
        self.audit_service.log(
            action_type="approval_execution",
            action_tier=int(claimed["action_tier"]),
            tool_name=str(claimed["tool_name"]),
            outcome=outcome,
            approved_by_owner=True,
            input_sources=["iphone_companion"],
            metadata={
                "action_request_id": claimed["id"],
                "payload_digest": claimed["payload_digest"],
                "result": result_summary,
            },
            error=str(error)[:2000],
        )
        self.sync_service.append(
            resource_type="approval",
            resource_id=str(claimed["id"]),
            operation=status,
            payload={**updated, "result": result_summary},
        )
        return updated

    def _expire_pending(self, *, action_request_id: str | None = None) -> None:
        now = utc_now()
        query = (
            """
            UPDATE action_requests
            SET status = 'expired', updated_at = ?
            WHERE status = 'pending' AND expires_at <= ?
            """
        )
        parameters: list[Any] = [now, now]
        if action_request_id is not None:
            query += " AND id = ?"
            parameters.append(action_request_id)
        with self.db.connection() as conn:
            conn.execute(query, tuple(parameters))

    def _row_to_dict(self, row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "action_tier": int(row["action_tier"]),
            "tool_name": row["tool_name"],
            "summary": row["summary"],
            "redacted_preview": self.db.loads(row["redacted_preview_json"]),
            "payload_digest": row["payload_digest"],
            "originating_goal": row["originating_goal"],
            "input_sources": self.db.loads(row["input_sources_json"]),
            "model": row["model"],
            "plan_id": row["plan_id"],
            "status": row["status"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "executed_at": row["executed_at"],
            "execution_outcome": row["execution_outcome"],
            "execution_error": row["execution_error"],
        }

    def _encrypt_payload(
        self,
        *,
        action_id: str,
        canonical_payload: bytes,
    ) -> dict[str, str]:
        key = self._payload_key()
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(
            nonce,
            canonical_payload,
            action_id.encode("utf-8"),
        )
        return {
            "algorithm": "AES-256-GCM",
            "nonce": _b64encode(nonce),
            "ciphertext": _b64encode(ciphertext),
        }

    def _decrypt_payload(
        self,
        *,
        action_id: str,
        encrypted_payload: dict[str, Any],
    ) -> dict[str, Any]:
        if encrypted_payload.get("algorithm") != "AES-256-GCM":
            raise ValueError("unsupported frozen approval payload")
        plaintext = AESGCM(self._payload_key()).decrypt(
            _b64decode(str(encrypted_payload["nonce"])),
            _b64decode(str(encrypted_payload["ciphertext"])),
            action_id.encode("utf-8"),
        )
        payload = json.loads(plaintext.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("frozen approval payload is not an object")
        return payload

    def _payload_key(self) -> bytes:
        try:
            key = _b64decode(self.vault_service.get_secret(_PAYLOAD_KEY_NAME))
        except KeyError:
            key = secrets.token_bytes(32)
            self.vault_service.set_secret(
                _PAYLOAD_KEY_NAME,
                _b64encode(key),
                "Project Q encrypted frozen approval payload key",
            )
        if len(key) != 32:
            raise ValueError("invalid frozen approval payload key")
        return key

    @staticmethod
    def _canonical_json(value: dict[str, Any]) -> bytes:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")

    @staticmethod
    def _decision(value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        return normalized

    @staticmethod
    def _require_active_device(row) -> None:
        if (
            row is None
            or row["status"] != "paired"
            or int(row["protocol_version"]) != 2
            or bool(row["legacy_repair_required"])
            or not str(row["approval_public_key"])
        ):
            raise PermissionError("companion device is not active for approvals")

    @staticmethod
    def _verify_signature(*, public_key: str, signature: str, message: bytes) -> None:
        try:
            key = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(),
                _b64decode(public_key),
            )
            key.verify(
                _b64decode(signature),
                message,
                ec.ECDSA(hashes.SHA256()),
            )
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise PermissionError("invalid companion approval signature") from exc

    def _verify_timestamp(self, timestamp: str) -> None:
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError as exc:
            raise PermissionError("invalid companion approval timestamp") from exc
        if parsed.tzinfo is None:
            raise PermissionError("companion approval timestamp must include a timezone")
        age = abs((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())
        if age > self.max_signature_age_seconds:
            raise PermissionError("stale companion approval timestamp")

    @staticmethod
    def _is_expired(expires_at: str) -> bool:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) <= datetime.now(UTC)
