from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class RelayStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._lock, self.connection() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS relay_devices (
                    device_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    token_expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    revoked_at TEXT,
                    revocation_reason TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS relay_devices_owner
                    ON relay_devices(owner_id, role);

                CREATE TABLE IF NOT EXISTS relay_envelopes (
                    message_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    sender_device_id TEXT NOT NULL,
                    recipient_device_id TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    envelope_json TEXT NOT NULL,
                    envelope_size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    UNIQUE(sender_device_id, message_id)
                );

                CREATE INDEX IF NOT EXISTS relay_envelopes_inbox
                    ON relay_envelopes(
                        recipient_device_id,
                        acknowledged_at,
                        created_at
                    );

                CREATE TABLE IF NOT EXISTS relay_presence (
                    device_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS relay_revocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    actor_device_id TEXT NOT NULL,
                    target_device_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS relay_apns_routes (
                    device_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    encrypted_token TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
