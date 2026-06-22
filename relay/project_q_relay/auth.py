from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any


class RelayAuthenticator:
    def __init__(self, store) -> None:
        self.store = store

    def authenticate(self, token: str) -> dict[str, Any]:
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with self.store.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM relay_devices
                WHERE revoked_at IS NULL
                """
            ).fetchall()
        matched = None
        for row in rows:
            if hmac.compare_digest(str(row["token_hash"]), token_hash):
                matched = row
        if matched is None:
            raise PermissionError("invalid relay bearer token")
        expires_at = datetime.fromisoformat(
            str(matched["token_expires_at"]).replace("Z", "+00:00")
        )
        if expires_at.astimezone(UTC) <= datetime.now(UTC):
            raise PermissionError("relay bearer token has expired")
        return {
            "device_id": matched["device_id"],
            "owner_id": matched["owner_id"],
            "role": matched["role"],
        }
