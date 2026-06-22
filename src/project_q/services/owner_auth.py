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
        gc_cutoff = (
            (datetime.now(UTC).replace(microsecond=0) - timedelta(days=7))
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
            # Garbage-collect long-dead sessions (expired/revoked over 7 days ago).
            conn.execute(
                """
                DELETE FROM owner_sessions
                WHERE created_at < ?
                  AND (revoked_at IS NOT NULL OR expires_at < ?)
                """,
                (gc_cutoff, now),
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

    def revoke_all_sessions(self, *, source: str = "kill_switch") -> int:
        now = utc_now()
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE owner_sessions
                SET revoked_at = ?
                WHERE revoked_at IS NULL
                """,
                (now,),
            )
        revoked = int(cursor.rowcount if cursor.rowcount is not None else 0)
        self.audit_service.log(
            action_type="owner_sessions_revoke_all",
            action_tier=2,
            tool_name="owner_auth",
            outcome="completed",
            approved_by_owner=True,
            input_sources=[source],
            metadata={"revoked_count": revoked, "revoked_at": now},
        )
        return revoked

    def passphrase_required(self) -> bool:
        """True when the owner has configured an optional login passphrase."""
        return self._passphrase_row() is not None

    def set_passphrase(self, passphrase: str) -> None:
        clean = str(passphrase or "")
        if len(clean) < 6:
            raise ValueError("owner passphrase must be at least 6 characters")
        salt = secrets.token_bytes(16)
        iterations = 200_000
        digest = hashlib.pbkdf2_hmac("sha256", clean.encode("utf-8"), salt, iterations)
        with self.db.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS owner_passphrase (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    salt TEXT NOT NULL, hash TEXT NOT NULL,
                    iterations INTEGER NOT NULL, updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO owner_passphrase (id, salt, hash, iterations, updated_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    salt = excluded.salt, hash = excluded.hash,
                    iterations = excluded.iterations, updated_at = excluded.updated_at
                """,
                (salt.hex(), digest.hex(), iterations, utc_now()),
            )

    def clear_passphrase(self) -> None:
        with self.db.connection() as conn:
            conn.execute("DROP TABLE IF EXISTS owner_passphrase")

    def verify_passphrase(self, passphrase: str) -> bool:
        row = self._passphrase_row()
        if row is None:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            str(passphrase or "").encode("utf-8"),
            bytes.fromhex(row["salt"]),
            int(row["iterations"]),
        )
        return hmac.compare_digest(digest.hex(), str(row["hash"]))

    def _passphrase_row(self):
        try:
            with self.db.connection() as conn:
                return conn.execute(
                    "SELECT salt, hash, iterations FROM owner_passphrase WHERE id = 1"
                ).fetchone()
        except Exception:  # noqa: BLE001  (table absent until a passphrase is set)
            return None

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
