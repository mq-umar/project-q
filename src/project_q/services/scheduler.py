from __future__ import annotations

import threading
import time
from typing import Any

from project_q.models import utc_now


class RoutineSchedulerService:
    """Background daemon that watches for routines with trigger_type='schedule'
    and fires them based on a simple interval configured in the routine's notes."""

    _CHECK_INTERVAL_SECONDS = 60

    def __init__(self, routine_service, routine_runner_service) -> None:
        self._routine_service = routine_service
        self._routine_runner = routine_runner_service
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_checked: str | None = None
        self._scheduled_count: int = 0
        self._next_runs: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> dict[str, Any]:
        """Start the background daemon thread (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="ProjectQRoutineScheduler",
            )
            self._thread.start()
        return self.status()

    def stop(self) -> None:
        """Signal the background thread to stop."""
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        with self._lock:
            self._thread = None

    def status(self) -> dict[str, Any]:
        """Return current scheduler status."""
        with self._lock:
            running = self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()
            return {
                "running": running,
                "scheduled_count": self._scheduled_count,
                "next_runs": list(self._next_runs),
                "last_checked": self._last_checked,
            }

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main scheduler loop: wakes every 60 s, evaluates due routines."""
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                pass  # never let an unhandled exception kill the thread
            self._stop_event.wait(self._CHECK_INTERVAL_SECONDS)

    def _tick(self) -> None:
        """Single evaluation pass: find due routines and run them."""
        now_str = utc_now()
        with self._lock:
            self._last_checked = now_str

        try:
            routines = self._routine_service.list_all(limit=500)
        except Exception:  # noqa: BLE001
            return

        scheduled = [
            r for r in routines
            if r.get("trigger_type") == "schedule" and r.get("status") in {"active", "pending"}
        ]

        next_runs: list[dict[str, Any]] = []
        due: list[dict[str, Any]] = []

        for routine in scheduled:
            interval_minutes = self._parse_interval(routine)
            if interval_minutes is None:
                continue

            last_run_at = routine.get("last_run_at")
            minutes_since = self._minutes_since(last_run_at)

            if minutes_since is None or minutes_since >= interval_minutes:
                due.append(routine)
            else:
                minutes_until = interval_minutes - minutes_since
                next_runs.append({
                    "routine_id": routine["id"],
                    "routine_name": routine["name"],
                    "interval_minutes": interval_minutes,
                    "minutes_until_next_run": round(minutes_until, 1),
                })

        with self._lock:
            self._scheduled_count = len(scheduled)
            self._next_runs = next_runs

        for routine in due:
            try:
                self._routine_runner.run(routine["id"], owner_approved=False)
            except Exception:  # noqa: BLE001
                pass  # isolate failures per-routine

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_interval(routine: dict[str, Any]) -> float | None:
        """Return schedule_interval_minutes from notes or metadata, or None."""
        # Check metadata dict (stored as arbitrary JSON on the routine)
        metadata = routine.get("metadata") or {}
        if isinstance(metadata, dict):
            val = metadata.get("schedule_interval_minutes")
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass

        # Check notes field: look for a line like "schedule_interval_minutes=30"
        notes = str(routine.get("notes") or "")
        for line in notes.splitlines():
            line = line.strip()
            if line.lower().startswith("schedule_interval_minutes"):
                parts = line.split("=", 1)
                if len(parts) == 2:
                    try:
                        return float(parts[1].strip())
                    except (TypeError, ValueError):
                        pass

        return None

    @staticmethod
    def _minutes_since(timestamp: str | None) -> float | None:
        """Return minutes elapsed since *timestamp* (ISO-8601 UTC), or None if never run."""
        if not timestamp:
            return None
        from datetime import UTC, datetime

        try:
            # Handle both "Z" suffix and "+00:00"
            ts = timestamp.rstrip("Z").replace("+00:00", "")
            dt = datetime.fromisoformat(ts).replace(tzinfo=UTC)
            delta = datetime.now(UTC) - dt
            return delta.total_seconds() / 60.0
        except (ValueError, TypeError):
            return None
