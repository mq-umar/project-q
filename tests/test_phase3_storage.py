from __future__ import annotations

import shutil
import sqlite3
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from project_q.storage import Database


class Phase3StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "project_q.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_connections_use_wal_busy_timeout_and_thirty_second_timeout(self) -> None:
        db = Database(self.db_path)
        original_connect = sqlite3.connect
        calls: list[tuple[tuple, dict]] = []

        def recording_connect(*args, **kwargs):
            calls.append((args, kwargs))
            return original_connect(*args, **kwargs)

        with patch("project_q.storage.sqlite3.connect", side_effect=recording_connect):
            with db.connection() as conn:
                journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

        self.assertEqual(journal_mode.lower(), "wal")
        self.assertEqual(busy_timeout, 30_000)
        self.assertEqual(calls[-1][1]["timeout"], 30)

    def test_every_connection_enforces_foreign_keys(self) -> None:
        db = Database(self.db_path)
        with db.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            conn.execute("CREATE TABLE parents (id TEXT PRIMARY KEY)")
            conn.execute(
                """
                CREATE TABLE children (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT NOT NULL REFERENCES parents(id)
                )
                """
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO children (id, parent_id) VALUES (?, ?)",
                    ("child-1", "missing-parent"),
                )

    def test_migrations_are_idempotent_and_recorded_once(self) -> None:
        first = Database(self.db_path)
        with first.connection() as conn:
            initial_versions = [
                row["version"]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]

        Database(self.db_path)
        Database(self.db_path)

        with first.connection() as conn:
            final_versions = [
                row["version"]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
            duplicates = conn.execute(
                """
                SELECT version, COUNT(*) AS count
                FROM schema_migrations
                GROUP BY version
                HAVING COUNT(*) > 1
                """
            ).fetchall()

        self.assertEqual(final_versions, initial_versions)
        self.assertEqual(duplicates, [])

    def test_concurrent_writers_complete_without_database_locked_errors(self) -> None:
        db = Database(self.db_path)
        writer_count = 8
        writes_per_thread = 20
        barrier = threading.Barrier(writer_count)
        errors: list[BaseException] = []

        def write_rows(writer_index: int) -> None:
            try:
                barrier.wait(timeout=5)
                for item_index in range(writes_per_thread):
                    with db.connection() as conn:
                        conn.execute(
                            """
                            INSERT INTO tasks (
                                id, title, description, priority, status, due_at,
                                source, metadata_json, created_at, updated_at
                            ) VALUES (?, ?, '', 3, 'pending', NULL, 'test', '{}', ?, ?)
                            """,
                            (
                                f"task-{writer_index}-{item_index}",
                                f"Writer {writer_index} item {item_index}",
                                "2026-06-14T00:00:00Z",
                                "2026-06-14T00:00:00Z",
                            ),
                        )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=write_rows, args=(index,))
            for index in range(writer_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        with db.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(count, writer_count * writes_per_thread)


if __name__ == "__main__":
    unittest.main()
