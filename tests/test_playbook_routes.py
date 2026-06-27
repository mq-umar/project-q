"""Route tests for owner-gated playbook listing + promotion."""
from __future__ import annotations

import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.models import MemoryCreate
from project_q.server import ProjectQHandler


class PlaybookRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            project_name="Playbook Route Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            port=8903,
        )
        self.app = create_application(config)
        handler = type("PlaybookTestHandler", (ProjectQHandler,), {"app": self.app})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass
        sd = getattr(self.app, "shutdown_services", None)
        if callable(sd):
            try:
                sd()
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _cookie(self) -> str:
        resp = urllib.request.urlopen(f"{self.base}/", timeout=10)
        return resp.headers["Set-Cookie"].split(";", 1)[0]

    def _req(self, method: str, path: str, cookie: str | None = None, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def test_playbook_routes_require_owner_then_promote(self) -> None:
        cookie = self._cookie()
        self.app.memory.create(
            MemoryCreate(
                text="write then commit",
                kind="procedural",
                tags=["playbook_candidate"],
                metadata={"tool_sequence": ["filesystem.write_file"]},
                owner_confirmed=False,
            )
        )
        # Unauthenticated -> 401
        status, _ = self._req("GET", "/api/memories/playbooks")
        self.assertEqual(status, 401)
        # Authenticated list
        status, data = self._req("GET", "/api/memories/playbooks", cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["playbooks"]), 1)
        memory_id = data["playbooks"][0]["memory_id"]
        # Promote -> 201, creates a routine
        before = len(self.app.routines.list_all(limit=100))
        status, _ = self._req("POST", f"/api/memories/playbooks/{memory_id}/promote", cookie)
        self.assertEqual(status, 201)
        self.assertEqual(len(self.app.routines.list_all(limit=100)), before + 1)


if __name__ == "__main__":
    unittest.main()
