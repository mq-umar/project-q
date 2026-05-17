from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, byref, c_char
from ctypes.wintypes import DWORD, LPWSTR
from typing import Any

from project_q.models import utc_now
from project_q.storage import Database


class DATA_BLOB(Structure):
    _fields_ = [("cbData", DWORD), ("pbData", POINTER(c_char))]


class VaultService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.is_windows = hasattr(ctypes, "windll")

    def list_secret_names(self) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT name, description, created_at, updated_at
                FROM secrets
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [
            {
                "name": row["name"],
                "description": row["description"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def set_secret(self, name: str, value: str, description: str = "") -> None:
        encrypted = self._encrypt(value.encode("utf-8"))
        now = utc_now()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO secrets (name, encrypted_blob, description, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    encrypted_blob = excluded.encrypted_blob,
                    description = excluded.description,
                    updated_at = excluded.updated_at
                """,
                (name, encrypted, description, now, now),
            )

    def get_secret(self, name: str) -> str:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT encrypted_blob FROM secrets WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            raise KeyError(name)
        return self._decrypt(row["encrypted_blob"]).decode("utf-8")

    def delete_secret(self, name: str) -> None:
        with self.db.connection() as conn:
            conn.execute("DELETE FROM secrets WHERE name = ?", (name,))

    def _encrypt(self, plaintext: bytes) -> bytes:
        if not self.is_windows:
            return plaintext
        in_buffer = ctypes.create_string_buffer(plaintext)
        in_blob = DATA_BLOB(len(plaintext), in_buffer)
        out_blob = DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if not crypt32.CryptProtectData(
            byref(in_blob),
            LPWSTR(),
            None,
            None,
            None,
            0,
            byref(out_blob),
        ):
            raise OSError("CryptProtectData failed")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)

    def _decrypt(self, encrypted: bytes) -> bytes:
        if not self.is_windows:
            return encrypted
        in_buffer = ctypes.create_string_buffer(encrypted)
        in_blob = DATA_BLOB(len(encrypted), in_buffer)
        out_blob = DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if not crypt32.CryptUnprotectData(
            byref(in_blob),
            LPWSTR(),
            None,
            None,
            None,
            0,
            byref(out_blob),
        ):
            raise OSError("CryptUnprotectData failed")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
