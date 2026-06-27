"""Tests for raw-artifact retention (PRD §9.6 auto-expiry)."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from project_q.services.retention import prune_expired_artifacts


class ArtifactRetentionTests(unittest.TestCase):
    def test_prunes_old_keeps_recent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            old, new = d / "screenshot-old.png", d / "screenshot-new.png"
            old.write_bytes(b"x")
            new.write_bytes(b"y")
            now = datetime.now(UTC)
            os.utime(old, ((now - timedelta(hours=48)).timestamp(),) * 2)
            os.utime(new, ((now - timedelta(hours=1)).timestamp(),) * 2)
            result = prune_expired_artifacts(d, retention_hours=24, now=now)
            self.assertEqual(result["pruned"], 1)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())

    def test_disabled_when_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            f = d / "a.png"
            f.write_bytes(b"x")
            os.utime(f, (0, 0))
            result = prune_expired_artifacts(d, retention_hours=0)
            self.assertEqual(result.get("skipped"), "disabled")
            self.assertTrue(f.exists())

    def test_missing_dir_is_safe(self) -> None:
        result = prune_expired_artifacts(Path(tempfile.gettempdir()) / "pq_nope_xyz123", retention_hours=24)
        self.assertEqual(result["pruned"], 0)

    def test_subdirectories_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            sub = d / "sub"
            sub.mkdir()
            os.utime(sub, ((datetime.now(UTC) - timedelta(hours=48)).timestamp(),) * 2)
            result = prune_expired_artifacts(d, retention_hours=24)
            self.assertEqual(result["pruned"], 0)
            self.assertTrue(sub.exists())


if __name__ == "__main__":
    unittest.main()
