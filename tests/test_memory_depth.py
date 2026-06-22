"""Tests for the Phase 3 memory-depth features.

Covers:
- source-memory create + read defaults (new depth columns)
- external-origin trust defaults
- embedding fallback path returns lexical results without Ollama (the default
  here — no provider running)
- hybrid rerank with a stubbed embedding transport
- playbook list + promote, owner-gated PermissionError

Built on the same full-application harness the main suite uses
(create_application(AppConfig(...)) with a temp dir).
"""
from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.models import MemoryCreate, SettingsUpdate, TaskCreate
from project_q.services.embeddings import EmbeddingService, cosine_similarity
from project_q.services.memory import MemoryService


class MemoryDepthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            project_name="Project Q Memory Depth Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            port=8902,
        )
        self.app = create_application(config)

    def tearDown(self) -> None:
        shutdown = getattr(self.app, "shutdown_services", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    # ── migration v4 columns exist after schema apply ───────────────────────
    def test_migration_adds_memory_depth_columns(self) -> None:
        with self.app.db.connection() as conn:
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
        for column in (
            "embedding_json",
            "source_type",
            "trust_level",
            "inferred",
            "evidence_json",
        ):
            self.assertIn(column, columns)

    # ── source-memory create + read defaults ────────────────────────────────
    def test_create_default_depth_fields(self) -> None:
        created = self.app.memory.create(MemoryCreate(text="owner stated fact"))
        fetched = self.app.memory.get(created["id"])
        self.assertEqual(fetched["source_type"], "owner")
        self.assertEqual(fetched["trust_level"], "trusted")
        self.assertFalse(fetched["inferred"])
        self.assertEqual(fetched["evidence"], [])

    # ── external-origin trust defaults ──────────────────────────────────────
    def test_external_origin_trust_defaults(self) -> None:
        created = self.app.memory.create(
            MemoryCreate(
                text="scraped from a website",
                source="import",
                source_type="external",
                trust_level="untrusted",
                inferred=True,
                evidence=[{"url": "https://example.com"}],
                owner_confirmed=False,
            )
        )
        fetched = self.app.memory.get(created["id"])
        self.assertEqual(fetched["source_type"], "external")
        self.assertEqual(fetched["trust_level"], "untrusted")
        self.assertTrue(fetched["inferred"])
        self.assertEqual(fetched["evidence"], [{"url": "https://example.com"}])
        self.assertFalse(fetched["owner_confirmed"])

    # ── embedding fallback: no Ollama → lexical results still returned ───────
    def test_search_falls_back_to_lexical_without_ollama(self) -> None:
        # Provider disabled by default in this environment → embed() returns None.
        self.assertFalse(self.app.memory._embedding_service.is_enabled())
        self.app.memory.create(MemoryCreate(text="the quick brown fox jumps"))
        self.app.memory.create(MemoryCreate(text="something unrelated entirely"))
        results = self.app.memory.search("brown fox")
        self.assertTrue(results)
        self.assertTrue(any("brown fox" in r["text"] for r in results))

    def test_embed_returns_none_when_provider_disabled(self) -> None:
        self.assertIsNone(self.app.memory._embedding_service.embed("anything"))

    def test_embed_returns_none_on_transport_failure(self) -> None:
        def boom(url, body, headers, timeout):  # noqa: ANN001
            raise OSError("connection refused")

        svc = EmbeddingService(self.app.settings, transport=boom)
        self.app.settings.update(
            SettingsUpdate(provider_enabled=True, provider_type="ollama")
        )
        self.assertIsNone(svc.embed("anything"))

    # ── hybrid rerank with a stubbed embedding transport ─────────────────────
    def test_hybrid_rerank_with_stubbed_embedding(self) -> None:
        # Deterministic embeddings keyed on substring presence, served via a
        # stub transport so no live Ollama is needed.
        def stub_transport(url, body, headers, timeout):  # noqa: ANN001
            import json as _json

            prompt = _json.loads(body.decode("utf-8")).get("prompt", "").lower()
            # 2-D vector: axis 0 = "alpha" topic, axis 1 = "beta" topic.
            vec = [
                1.0 if "alpha" in prompt else 0.0,
                1.0 if "beta" in prompt else 0.0,
            ]
            # Ensure a non-zero norm so cosine is well-defined.
            if vec == [0.0, 0.0]:
                vec = [0.01, 0.01]
            return {"embedding": vec}

        embedding_service = EmbeddingService(self.app.settings, transport=stub_transport)
        self.app.settings.update(
            SettingsUpdate(provider_enabled=True, provider_type="ollama")
        )
        memory = MemoryService(
            self.app.db,
            self.app.settings,
            embedding_service=embedding_service,
        )
        # Both rows lexically match "topic"; embeddings should rank alpha first
        # for an alpha-flavored query.
        memory.create(MemoryCreate(text="topic alpha document"))
        memory.create(MemoryCreate(text="topic beta document"))
        results = memory.search("topic alpha")
        self.assertTrue(results)
        self.assertIn("alpha", results[0]["text"])

    def test_cosine_similarity_pure_python(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertEqual(cosine_similarity([], [1.0]), 0.0)
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)

    # ── playbook list + promote (owner-gated PermissionError) ───────────────
    def _make_playbook_candidate(self) -> str:
        task = self.app.tasks.create(TaskCreate(title="Do the thing", description="d"))
        # Mark completed and log audit entries referencing the task so the
        # reflection produces a clean, promotable playbook.
        self.app.tasks.update(task["id"], _completed_update())
        self.app.audit.log(
            action_type="tool_call",
            action_tier=0,
            tool_name="filesystem.list_directory",
            outcome="completed",
            input_sources=["owner"],
            metadata={"task_id": task["id"]},
        )
        self.app.audit.log(
            action_type="tool_call",
            action_tier=0,
            tool_name="filesystem.read_file",
            outcome="completed",
            input_sources=["owner"],
            metadata={"task_id": task["id"]},
        )
        reflection = self.app.learning.reflect_on_task(task["id"])
        self.assertTrue(reflection["playbook"]["promotable"])
        return reflection["playbook_memory_id"]

    def test_list_playbook_candidates(self) -> None:
        memory_id = self._make_playbook_candidate()
        candidates = self.app.memory.list_playbook_candidates()
        self.assertTrue(any(c["memory_id"] == memory_id for c in candidates))
        candidate = next(c for c in candidates if c["memory_id"] == memory_id)
        self.assertTrue(candidate["tool_sequence"])

    def test_promote_playbook_requires_owner_confirmation(self) -> None:
        memory_id = self._make_playbook_candidate()
        with self.assertRaises(PermissionError):
            self.app.memory.promote_playbook(memory_id, owner_confirmed=False)

    def test_promote_playbook_creates_trusted_routine(self) -> None:
        memory_id = self._make_playbook_candidate()
        result = self.app.memory.promote_playbook(memory_id, owner_confirmed=True)
        self.assertTrue(result["trusted"])
        routine = next(
            r for r in self.app.routines.list_all(limit=100)
            if r["id"] == result["routine_id"]
        )
        self.assertTrue(routine["trusted"])
        # Source memory is now owner-confirmed and tagged promoted.
        promoted = self.app.memory.get(memory_id)
        self.assertTrue(promoted["owner_confirmed"])
        self.assertIn("playbook_promoted", promoted["tags"])


def _completed_update():
    from project_q.models import TaskUpdate

    return TaskUpdate(status="completed")


if __name__ == "__main__":
    unittest.main()
