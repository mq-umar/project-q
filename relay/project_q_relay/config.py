from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RelayConfig:
    database_path: Path
    max_envelope_bytes: int = 512 * 1024
    envelope_retention_seconds: int = 7 * 24 * 60 * 60
    presence_ttl_seconds: int = 120
    bootstrap_token: str = ""
    owner_id: str = "project-q-owner"
    apns_route_key: bytes | None = None
    apns_team_id: str = ""
    apns_key_id: str = ""
    apns_bundle_id: str = ""
    apns_private_key_file: Path | None = None

    @classmethod
    def from_environment(cls) -> "RelayConfig":
        route_key_text = os.environ.get(
            "PROJECT_Q_RELAY_APNS_ROUTE_KEY",
            "",
        ).strip()
        route_key = None
        if route_key_text:
            try:
                route_key = base64.urlsafe_b64decode(
                    route_key_text + ("=" * (-len(route_key_text) % 4))
                )
            except ValueError as exc:
                raise ValueError(
                    "PROJECT_Q_RELAY_APNS_ROUTE_KEY must be URL-safe base64"
                ) from exc
            if len(route_key) != 32:
                raise ValueError(
                    "PROJECT_Q_RELAY_APNS_ROUTE_KEY must decode to 32 bytes"
                )
        private_key_text = os.environ.get(
            "PROJECT_Q_APNS_PRIVATE_KEY_FILE",
            "",
        ).strip()
        return cls(
            database_path=Path(
                os.environ.get("PROJECT_Q_RELAY_DB", "/data/project_q_relay.db")
            ),
            max_envelope_bytes=int(
                os.environ.get("PROJECT_Q_RELAY_MAX_ENVELOPE_BYTES", 512 * 1024)
            ),
            envelope_retention_seconds=int(
                os.environ.get(
                    "PROJECT_Q_RELAY_RETENTION_SECONDS",
                    7 * 24 * 60 * 60,
                )
            ),
            presence_ttl_seconds=min(
                120,
                max(
                    15,
                    int(os.environ.get("PROJECT_Q_RELAY_PRESENCE_TTL_SECONDS", 120)),
                ),
            ),
            bootstrap_token=os.environ.get(
                "PROJECT_Q_RELAY_BOOTSTRAP_TOKEN",
                "",
            ).strip(),
            owner_id=(
                os.environ.get("PROJECT_Q_RELAY_OWNER_ID", "project-q-owner").strip()
                or "project-q-owner"
            ),
            apns_route_key=route_key,
            apns_team_id=os.environ.get("PROJECT_Q_APNS_TEAM_ID", "").strip(),
            apns_key_id=os.environ.get("PROJECT_Q_APNS_KEY_ID", "").strip(),
            apns_bundle_id=os.environ.get(
                "PROJECT_Q_APNS_BUNDLE_ID",
                "",
            ).strip(),
            apns_private_key_file=(
                Path(private_key_text) if private_key_text else None
            ),
        )
