"""Tests for the declarative plugin/skill system.

Service-level tests use the full-application harness (create_application over a
temp dir, mirroring tests/test_audit_fixes.py). The owner-session route test uses
a ThreadingHTTPServer + cookie harness mirroring tests/test_phase3_workflow_api.py.
"""
from __future__ import annotations

import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pydantic import ValidationError

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.models import SettingsUpdate
from project_q.server import ProjectQHandler
from project_q.services.plugins import MAX_PLUGIN_BYTES, PluginManager, PluginManifest, PluginTool


def _good_http_manifest(plugin_id: str = "weather-1", url: str = "https://api.example.com/{q}") -> dict:
    return {
        "id": plugin_id,
        "name": "Weather",
        "description": "fetch weather",
        "type": "http",
        "method": "GET",
        "url": url,
        "tier": 1,
    }


def _good_shell_manifest(plugin_id: str = "lister") -> dict:
    return {
        "id": plugin_id,
        "name": "Lister",
        "description": "list things",
        "type": "shell",
        "command": "echo hi",
        "tier": 1,
    }


class PluginManifestValidationTests(unittest.TestCase):
    def test_manifest_validation_good(self) -> None:
        http = PluginManifest.model_validate(_good_http_manifest())
        self.assertEqual(http.type, "http")
        self.assertEqual(http.url, "https://api.example.com/{q}")
        self.assertEqual(http.tier, 1)
        shell = PluginManifest.model_validate(_good_shell_manifest())
        self.assertEqual(shell.type, "shell")
        self.assertEqual(shell.command, "echo hi")

    def test_manifest_rejects_bad_id(self) -> None:
        with self.assertRaises(ValidationError):
            PluginManifest.model_validate(_good_http_manifest(plugin_id="BAD ID!"))
        with self.assertRaises(ValidationError):
            PluginManifest.model_validate(_good_http_manifest(plugin_id="x"))

    def test_manifest_rejects_non_http_scheme(self) -> None:
        with self.assertRaises(ValidationError):
            PluginManifest.model_validate(_good_http_manifest(url="file:///etc/passwd"))

    def test_manifest_rejects_tier3_self_declared_http(self) -> None:
        bad = _good_http_manifest()
        bad["tier"] = 3
        with self.assertRaises(ValidationError):
            PluginManifest.model_validate(bad)

    def test_manifest_rejects_extra_keys(self) -> None:
        bad = _good_http_manifest()
        bad["python_path"] = "os.system"
        with self.assertRaises(ValidationError):
            PluginManifest.model_validate(bad)

    def test_shell_plugin_forced_to_tier3(self) -> None:
        manifest = PluginManifest.model_validate(_good_shell_manifest())
        self.assertEqual(manifest.tier, 3)
        tool = PluginTool(manifest)
        self.assertEqual(tool.definition.tier, 3)
        self.assertEqual(tool.definition.tool_id, "plugin.lister")


class _StubSettings:
    def __init__(self, network_policy: str) -> None:
        self._policy = network_policy

    def get_all(self) -> dict:
        return {"network_policy": self._policy}


class _EchoHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"path": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence test noise
        pass


class PluginToolExecuteTests(unittest.TestCase):
    def test_http_plugin_executes_loopback(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_port
            manifest = PluginManifest.model_validate(
                _good_http_manifest(url=f"http://127.0.0.1:{port}/echo/{{q}}")
            )
            tool = PluginTool(manifest, settings_service=_StubSettings("selected_services"))
            result = tool.execute({"q": "abc"})
            self.assertEqual(result["status_code"], 200)
            self.assertIn("/echo/abc", result["body"])
            self.assertEqual(result["url"], f"http://127.0.0.1:{port}/echo/abc")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_plugin_local_only_blocks_non_loopback(self) -> None:
        manifest = PluginManifest.model_validate(
            _good_http_manifest(url="https://example.com/data")
        )
        tool = PluginTool(manifest, settings_service=_StubSettings("local_only"))
        result = tool.execute({})
        self.assertIn("error", result)
        self.assertIn("local_only", result["error"])
        self.assertNotIn("status_code", result)

    def test_shell_plugin_executes(self) -> None:
        manifest = PluginManifest.model_validate(_good_shell_manifest())
        tool = PluginTool(manifest)
        result = tool.execute({})
        self.assertEqual(result["returncode"], 0)
        self.assertIn("hi", result["stdout"])

    def test_missing_placeholder_does_not_raise(self) -> None:
        manifest = PluginManifest.model_validate(
            _good_http_manifest(url="https://example.com/{missing}")
        )
        tool = PluginTool(manifest, settings_service=_StubSettings("local_only"))
        # local_only blocks, but the point is execute() does not raise on missing key.
        result = tool.execute({})
        self.assertIn("error", result)


class PluginAppIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = AppConfig(
            project_name="Project Q Plugin Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            port=8902,
        )
        self.app = create_application(self.config)

    def tearDown(self) -> None:
        shutdown = getattr(self.app, "shutdown_services", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def test_install_registers_usable_plugin_tool(self) -> None:
        definition = self.app.plugins.install(_good_http_manifest())
        self.assertEqual(definition["tool_id"], "plugin.weather-1")
        # Hot-register like the route does.
        tool = self.app.plugins.get_tool("weather-1")
        self.app.tools.register(tool)
        self.assertIs(self.app.tools.get("plugin.weather-1"), tool)

        # tier 1 http auto-approves at default profile.
        decision = self.app.policy.authorize_tool(
            tier=tool.definition.tier, owner_approved=False, input_sources=["owner"]
        )
        self.assertTrue(decision.allowed)

    def test_install_shell_is_tier3_gated(self) -> None:
        self.app.plugins.install(_good_shell_manifest())
        tool = self.app.plugins.get_tool("lister")
        self.app.tools.register(tool)
        self.assertEqual(tool.definition.tier, 3)
        blocked = self.app.policy.authorize_tool(
            tier=tool.definition.tier, owner_approved=False, input_sources=["owner"]
        )
        self.assertFalse(blocked.allowed)
        allowed = self.app.policy.authorize_tool(
            tier=tool.definition.tier, owner_approved=True, input_sources=["owner"]
        )
        self.assertTrue(allowed.allowed)

    def test_install_rejects_builtin_collision(self) -> None:
        # plugin ids cannot match a built-in tool id directly.
        bad = _good_http_manifest(plugin_id="shell")
        bad["id"] = "shell"
        # Force a collision: register a fake built-in under 'plugin.collide'.
        self.app.plugins.builtin_tool_ids.add("plugin.collide")
        clash = _good_http_manifest(plugin_id="collide")
        with self.assertRaises(ValueError):
            self.app.plugins.install(clash)

    def test_install_rejects_duplicate(self) -> None:
        self.app.plugins.install(_good_http_manifest())
        with self.assertRaises(ValueError):
            self.app.plugins.install(_good_http_manifest())

    def test_install_rejects_invalid_manifest(self) -> None:
        with self.assertRaises(ValueError):
            self.app.plugins.install({"id": "BAD ID", "name": "x", "type": "http"})

    def test_remove_unregisters(self) -> None:
        self.app.plugins.install(_good_http_manifest())
        tool = self.app.plugins.get_tool("weather-1")
        self.app.tools.register(tool)
        self.assertIn("plugin.weather-1", self.app.tools.tools)
        removed = self.app.plugins.remove("weather-1")
        self.app.tools.tools.pop("plugin.weather-1", None)
        self.assertTrue(removed)
        self.assertNotIn("plugin.weather-1", self.app.tools.tools)
        path = self.config.data_root / "plugins" / "weather-1.json"
        self.assertFalse(path.exists())


class PluginRegistryIsolationTests(unittest.TestCase):
    def test_malformed_manifest_does_not_break_registry(self) -> None:
        root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        plugins_dir = root / ".project_q" / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        # Write a malformed JSON file and an oversized file BEFORE app creation.
        (plugins_dir / "broken.json").write_text("{ not valid json", encoding="utf-8")
        (plugins_dir / "huge.json").write_text("x" * (MAX_PLUGIN_BYTES + 10), encoding="utf-8")
        config = AppConfig(
            project_name="Project Q Plugin Isolation",
            workspace_root=root,
            data_root=root / ".project_q",
            db_path=root / ".project_q" / "project_q.db",
            port=8903,
        )
        app = create_application(config)
        try:
            self.assertIn("shell.run_command", app.tools.tools)
            self.assertFalse(any(t.startswith("plugin.") for t in app.tools.tools))
        finally:
            try:
                app.shutdown_services()
            except Exception:
                pass
            shutil.rmtree(root, ignore_errors=True)


class PluginHttpRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.app = create_application(
            AppConfig(
                project_name="Project Q Plugin Route Test",
                workspace_root=self.root,
                data_root=self.root / ".project_q",
                db_path=self.root / ".project_q" / "project_q.db",
                port=8904,
            )
        )
        handler = type("PluginRouteHandler", (ProjectQHandler,), {"app": self.app})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        response = urllib.request.urlopen(f"{self.base_url}/", timeout=10)
        self.cookie = response.headers["Set-Cookie"].split(";", 1)[0]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        try:
            self.app.shutdown_services()
        except Exception:
            pass
        shutil.rmtree(self.root, ignore_errors=True)

    def request(self, method: str, path: str, payload=None, *, owner=True):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if owner:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method, headers=headers
        )
        response = urllib.request.urlopen(request, timeout=10)
        return response.status, json.loads(response.read().decode("utf-8"))

    def test_get_plugins_requires_owner_session(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request("GET", "/api/plugins", owner=False)
        self.assertEqual(captured.exception.code, 401)
        status, payload = self.request("GET", "/api/plugins")
        self.assertEqual(status, 200)
        self.assertIn("items", payload)

    def test_install_hot_registers_and_appears_in_tools(self) -> None:
        status, definition = self.request("POST", "/api/plugins", _good_http_manifest())
        self.assertEqual(status, 201)
        self.assertEqual(definition["tool_id"], "plugin.weather-1")
        _, tools = self.request("GET", "/api/tools")
        ids = {item["tool_id"] for item in tools["items"]}
        self.assertIn("plugin.weather-1", ids)
        # Delete drops it from the live registry.
        status, _ = self.request("DELETE", "/api/plugins/weather-1")
        self.assertEqual(status, 200)
        _, tools_after = self.request("GET", "/api/tools")
        ids_after = {item["tool_id"] for item in tools_after["items"]}
        self.assertNotIn("plugin.weather-1", ids_after)


if __name__ == "__main__":
    unittest.main()
