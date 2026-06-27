"""A/B experiment tracking — assign variants, record outcomes, rank by a
Wilson-scored success rate, and promote a winner once the data supports it.

Adapted for Project Q from the personal-use-licensed "JARVIS" assistant by Ethan
Rogers; generalized from prompt-template A/B testing to arbitrary variant keys and
backed by Project Q's shared SQLite database (lazy table, no core migration).
"""
from __future__ import annotations

import math
import random
from typing import Any

from project_q.models import utc_now
from project_q.storage import Database

_MIN_TASKS_FOR_WINNER = 20
_MIN_RATE_DIFFERENCE = 10.0  # percentage points


class ExperimentService:
    def __init__(self, db: Database) -> None:
        self.db = db
        with self.db.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ab_experiments (
                    id TEXT PRIMARY KEY,
                    experiment_key TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    success INTEGER,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ab_key ON ab_experiments(experiment_key)"
            )

    def assign(self, experiment_key: str, variants: list[str]) -> dict[str, Any]:
        """Randomly assign one of the variants and record the assignment."""
        clean = [str(v).strip() for v in (variants or []) if str(v).strip()]
        if not clean:
            raise ValueError("at least one variant is required")
        variant = random.choice(clean)  # noqa: S311 - experiment assignment, not security
        experiment_id = self.db.make_id("abexp")
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO ab_experiments (id, experiment_key, variant, created_at) "
                "VALUES (?, ?, ?, ?)",
                (experiment_id, str(experiment_key)[:120], variant, utc_now()),
            )
        return {"experiment_id": experiment_id, "experiment_key": str(experiment_key)[:120], "variant": variant}

    def record(self, experiment_id: str, success: bool) -> bool:
        """Record the outcome of an assignment (idempotent — first write wins)."""
        with self.db.connection() as conn:
            cursor = conn.execute(
                "UPDATE ab_experiments SET success = ?, completed_at = ? "
                "WHERE id = ? AND success IS NULL",
                (1 if success else 0, utc_now(), experiment_id),
            )
            return cursor.rowcount == 1

    def stats(self, experiment_key: str) -> dict[str, dict[str, Any]]:
        """Per-variant success rate with a Wilson ~95% confidence interval."""
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT variant, success, COUNT(*) AS cnt FROM ab_experiments "
                "WHERE experiment_key = ? AND success IS NOT NULL "
                "GROUP BY variant, success",
                (experiment_key,),
            ).fetchall()
        agg: dict[str, dict[str, int]] = {}
        for row in rows:
            bucket = agg.setdefault(row["variant"], {"passed": 0, "failed": 0})
            bucket["passed" if row["success"] else "failed"] += int(row["cnt"])
        out: dict[str, dict[str, Any]] = {}
        for variant, bucket in agg.items():
            total = bucket["passed"] + bucket["failed"]
            rate = (bucket["passed"] / total * 100) if total else 0.0
            low, high = self._wilson(bucket["passed"], total)
            out[variant] = {
                "variant": variant,
                "success_rate": round(rate, 2),
                "total": total,
                "passed": bucket["passed"],
                "failed": bucket["failed"],
                "ci_low": low,
                "ci_high": high,
            }
        return out

    def winner(
        self,
        experiment_key: str,
        *,
        min_tasks: int = _MIN_TASKS_FOR_WINNER,
        min_gap: float = _MIN_RATE_DIFFERENCE,
    ) -> str | None:
        """Return the winning variant if >=2 variants have enough data and a gap."""
        qualified = [s for s in self.stats(experiment_key).values() if s["total"] >= min_tasks]
        if len(qualified) < 2:
            return None
        ranked = sorted(qualified, key=lambda s: s["success_rate"], reverse=True)
        if ranked[0]["success_rate"] - ranked[1]["success_rate"] >= min_gap:
            return ranked[0]["variant"]
        return None

    @staticmethod
    def _wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
        """Wilson score interval for a binomial proportion, as percentages."""
        if total == 0:
            return (0.0, 0.0)
        p = successes / total
        denom = 1 + z * z / total
        centre = (p + z * z / (2 * total)) / denom
        spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
        return (round(max(0.0, centre - spread) * 100, 2), round(min(1.0, centre + spread) * 100, 2))
