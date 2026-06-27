"""Raw-artifact retention (PRD §9.6: screenshots/audio auto-expire after a window)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def prune_expired_artifacts(
    artifacts_dir: Path,
    *,
    retention_hours: int = 24,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete raw screenshot/OCR artifacts older than the retention window.

    Best-effort and never raises. ``retention_hours <= 0`` disables pruning.
    Returns a summary dict.
    """
    retention_hours = int(retention_hours)
    if retention_hours <= 0:
        return {"pruned": 0, "retention_hours": retention_hours, "skipped": "disabled"}
    moment = now or datetime.now(UTC)
    cutoff = (moment - timedelta(hours=retention_hours)).timestamp()
    pruned = 0
    errors = 0
    try:
        if not artifacts_dir.exists():
            return {"pruned": 0, "retention_hours": retention_hours, "missing_dir": True}
        for path in artifacts_dir.iterdir():
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    pruned += 1
            except OSError:
                errors += 1
    except OSError:
        errors += 1
    return {"pruned": pruned, "retention_hours": retention_hours, "errors": errors}
