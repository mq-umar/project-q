from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from project_q.models import utc_now
from project_q.storage import Database


class OwnerAuthService:
    def __init__(self, db: Database, audit_service, session_ttl_hours: int = 8) -> None:
        self.db = db
        self.audit_service = audit_service
        self.session_ttl_hours = session_ttl_hours

    def create_session(self, *, source: str = "dashboard") -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        session_id = self.db.make_id("session")
        now = utc_now()
        expires_at = (
            (datetime.now(UTC).replace(microsecond=0) + timedelta(hours=self.session_ttl_hours))
            .isoformat()
            .replace("+00:00", "Z")
        )
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO owner_sessions (
                    id, token_hash, source, created_at, expires_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, self._hash_token(token), str(source or "dashboard")[:80], now, expires_at, now),
            )
        self.audit_service.log(
            action_type="owner_session_create",
            action_tier=0,
            tool_name="owner_auth",
            outcome="completed",
            approved_by_owner=True,
            input_sources=[source],
            metadata={"session_id": session_id, "expires_at": expires_at},
        )
        return {"id": session_id, "token": token, "expires_at": expires_at}

    def verify_session(self, token: str) -> dict[str, Any]:
        token_hash = self._hash_token(token)
        with self.db.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM owner_sessions
                WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
        if row is None or not hmac.compare_digest(row["token_hash"], token_hash):
            raise PermissionError("owner session is required")
        if row["revoked_at"]:
            raise PermissionError("owner session is revoked")
        if self._is_expired(row["expires_at"]):
            raise PermissionError("owner session expired")
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute("UPDATE owner_sessions SET last_seen_at = ? WHERE id = ?", (now, row["id"]))
        return {
            "id": row["id"],
            "source": row["source"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "last_seen_at": now,
        }

    def revoke_session(self, token: str) -> None:
        token_hash = self._hash_token(token)
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE owner_sessions SET revoked_at = ? WHERE token_hash = ?",
                (utc_now(), token_hash),
            )

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    @staticmethod
    def _is_expired(expires_at: str) -> bool:
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return True
        return parsed <= datetime.now(UTC)
