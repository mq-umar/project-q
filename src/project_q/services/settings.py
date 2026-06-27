from __future__ import annotations

from typing import Any

from project_q.models import SettingsUpdate, utc_now
from project_q.storage import Database


DEFAULT_SETTINGS = {
    "owner_name": "",
    "aggression_level": "operator",
    "auto_approve_tier": 1,
    "memory_mode": "standard",
    "voice_enabled": False,
    "sync_enabled": False,
    "notifications_enabled": True,
    "provider_enabled": False,
    "provider_type": "ollama",
    "model_name": "qwen3.5:9b",
    "model_base_url": "http://localhost:11434/api/chat",
    "model_secret_name": "",
    "ollama_model_routing_enabled": True,
    "ollama_general_model": "qwen3.5:9b",
    "ollama_coding_model": "qwen2.5-coder:7b",
    "ollama_reasoning_model": "deepseek-r1:7b",
    "ollama_fast_model": "llama3.1:8b",
    "provider_timeout_seconds": 120,
    "browser_headless": False,
    "browser_channel": "msedge",
    "browser_executable_path": "",
    "file_access_roots": [],
    "learning_enabled": False,
    "learning_interval_seconds": 300,
    "learning_max_cycles_per_start": 12,
    "kill_switch_active": False,
    "kill_switch_reason": "",
    "kill_switch_activated_at": "",
    "kill_switch_source": "",
    # §12.1 Behavior Profiles
    "approval_policy": "ask_on_risky",
    "proactive_mode": "active",
    "screen_context": "manual",
    "network_policy": "selected_services",
    "execution_environment": "sandbox_first",
    "voice_mode": "push_to_talk",
    # Retention & scheduler
    "memory_retention_days": 90,
    "artifact_retention_hours": 24,
    "git_workspace": "",
    "scheduler_enabled": True,
    "metrics_enabled": True,
    "outlook_enabled": False,
    "auto_reflect_on_tasks": False,
}


class SettingsService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._seed_defaults()

    def _seed_defaults(self) -> None:
        with self.db.connection() as conn:
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    """
                    INSERT INTO settings (key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO NOTHING
                    """,
                    (key, self.db.dumps(value), utc_now()),
                )

    def get_all(self) -> dict[str, Any]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM settings").fetchall()
        settings: dict[str, Any] = {}
        for row in rows:
            settings[row["key"]] = self.db.loads(row["value_json"])
        return settings

    def update(self, payload: SettingsUpdate) -> dict[str, Any]:
        updates = payload.model_dump(exclude_none=True)
        if not updates:
            return self.get_all()
        with self.db.connection() as conn:
            for key, value in updates.items():
                conn.execute(
                    """
                    INSERT INTO settings (key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        updated_at = excluded.updated_at
                    """,
                    (key, self.db.dumps(value), utc_now()),
                )
        return self.get_all()
