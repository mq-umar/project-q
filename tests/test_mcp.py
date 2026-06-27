"""Tests for the clean-room MCP stdio client.

Unit tests inject a FAKE in-memory connection (NO subprocess) via
``connection_factory`` so they are fast and deterministic. ONE integration test
drives a real :class:`StdioMCPConnection` against a tiny stub MCP server
subprocess, guarded by generous timeouts so it can never hang the suite.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from project_q.services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPTool,
    McpError,
    StdioMCPConnection,
)

_STUB = str(Path(__file__).resolve().parent / "_mcp_stub_server.py")


def _good_config(server_id: str = "stub-1", **overrides) -> dict:
    config = {
        "id": server_id,
        "name": "Stub Server",
        "description": "a stub",
        "command": "node",
        "args": ["server.js"],
        "tier": 2,
        "enabled": True,
    }
    config.update(overrides)
    return config


class _FakeConnection:
    """In-memory fake speaking the connection interface; NO subprocess.

    ``tools`` is the tools/list payload; ``raise_on`` lets a test force a failure
    at a given phase ('initialize', 'list_tools', 'call_tool').
    """

    def __init__(self, config: MCPServerConfig, tools=None, raise_on=None, call_result=None):
        self.config = config
        self._tools = tools if tools is not None else [{"name": "echo", "description": "echo it"}]
        self._raise_on = raise_on
        self._call_result = call_result if call_result is not None else {"content": [{"type": "text", "text": "ok"}]}
        self.closed = False
        self.calls: list[tuple] = []

    def initialize(self):
        if self._raise_on == "initialize":
            raise McpError("init failed")
        return {"protocolVersion": "2024-11-05"}

    def list_tools(self):
        if self._raise_on == "list_tools":
            raise McpError("list failed")
        return self._tools

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if self._raise_on == "call_tool":
            raise McpError("boom")
        return self._call_result

    def close(self):
        self.closed = True


class ConfigValidationTests(unittest.TestCase):
    def test_good_config(self):
        cfg = MCPServerConfig.model_validate(_good_config())
        self.assertEqual(cfg.id, "stub-1")
        self.assertEqual(cfg.tier, 2)
        self.assertTrue(cfg.enabled)

    def test_defaults(self):
        cfg = MCPServerConfig.model_validate({"id": "srv", "name": "S", "command": "python"})
        self.assertEqual(cfg.args, [])
        self.assertEqual(cfg.env, {})
        self.assertEqual(cfg.tier, 2)
        self.assertTrue(cfg.enabled)

    def test_bad_id_rejected(self):
        with self.assertRaises(ValidationError):
            MCPServerConfig.model_validate(_good_config(server_id="Bad Id!"))

    def test_reserved_id_rejected(self):
        with self.assertRaises(ValidationError):
            MCPServerConfig.model_validate(_good_config(server_id="con"))

    def test_empty_command_rejected(self):
        with self.assertRaises(ValidationError):
            MCPServerConfig.model_validate(_good_config(command="   "))

    def test_extra_key_rejected(self):
        with self.assertRaises(ValidationError):
            MCPServerConfig.model_validate(_good_config(secret="smuggled"))

    def test_tier_out_of_range_rejected(self):
        with self.assertRaises(ValidationError):
            MCPServerConfig.model_validate(_good_config(tier=5))


class ManagerInstallRemoveListTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.manager = MCPManager(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_then_list(self):
        stored = self.manager.install_server(_good_config())
        self.assertEqual(stored["id"], "stub-1")
        servers = self.manager.list_servers()
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["id"], "stub-1")

    def test_install_redacts_env_in_list(self):
        self.manager.install_server(_good_config(env={"TOKEN": "supersecret"}))
        servers = self.manager.list_servers()
        self.assertEqual(servers[0]["env"], {"TOKEN": "***"})

    def test_install_duplicate_rejected(self):
        self.manager.install_server(_good_config())
        with self.assertRaises(ValueError):
            self.manager.install_server(_good_config())

    def test_install_invalid_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.install_server({"id": "x"})  # missing name/command

    def test_remove(self):
        self.manager.install_server(_good_config())
        self.assertTrue(self.manager.remove_server("stub-1"))
        self.assertEqual(self.manager.list_servers(), [])

    def test_remove_unknown_returns_false(self):
        self.assertFalse(self.manager.remove_server("nope"))

    def test_remove_path_traversal_rejected(self):
        self.assertFalse(self.manager.remove_server("../../etc/passwd"))


class ConnectAllTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _manager(self, factory):
        return MCPManager(self.root, connection_factory=factory)

    def test_connect_all_builds_namespaced_tools_at_tier(self):
        manager = self._manager(lambda cfg: _FakeConnection(cfg))
        manager.install_server(_good_config(tier=2))
        tools = manager.connect_all()
        self.assertEqual(len(tools), 1)
        tool = tools[0]
        self.assertEqual(tool.definition.tool_id, "mcp.stub-1.echo")
        self.assertEqual(tool.definition.tier, 2)

    def test_connect_all_honors_custom_tier(self):
        manager = self._manager(lambda cfg: _FakeConnection(cfg))
        manager.install_server(_good_config(server_id="srv3", tier=3))
        tools = manager.connect_all()
        self.assertEqual(tools[0].definition.tier, 3)

    def test_disabled_server_skipped(self):
        manager = self._manager(lambda cfg: _FakeConnection(cfg))
        manager.install_server(_good_config(enabled=False))
        self.assertEqual(manager.connect_all(), [])

    def test_failing_connection_skipped_without_raising(self):
        manager = self._manager(lambda cfg: _FakeConnection(cfg, raise_on="initialize"))
        manager.install_server(_good_config())
        # Must not raise, and skips the bad server.
        self.assertEqual(manager.connect_all(), [])

    def test_one_bad_one_good(self):
        def factory(cfg):
            if cfg.id == "bad":
                return _FakeConnection(cfg, raise_on="list_tools")
            return _FakeConnection(cfg)

        manager = self._manager(factory)
        manager.install_server(_good_config(server_id="bad"))
        manager.install_server(_good_config(server_id="good"))
        tools = manager.connect_all()
        self.assertEqual([t.definition.tool_id for t in tools], ["mcp.good.echo"])

    def test_malformed_config_does_not_break_connect_all(self):
        manager = self._manager(lambda cfg: _FakeConnection(cfg))
        manager.install_server(_good_config(server_id="good"))
        # Drop a malformed JSON file into the config dir.
        bad = manager.config_dir / "broken.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        tools = manager.connect_all()
        self.assertEqual([t.definition.tool_id for t in tools], ["mcp.good.echo"])

    def test_close_all_closes_live_connections(self):
        created = []

        def factory(cfg):
            conn = _FakeConnection(cfg)
            created.append(conn)
            return conn

        manager = self._manager(factory)
        manager.install_server(_good_config())
        manager.connect_all()
        manager.close_all()
        self.assertTrue(all(c.closed for c in created))


class MCPToolExecuteTests(unittest.TestCase):
    def test_execute_returns_connection_result(self):
        cfg = MCPServerConfig.model_validate(_good_config())
        conn = _FakeConnection(cfg, call_result={"content": [{"type": "text", "text": "hi"}]})
        tool = MCPTool("stub-1", "echo", 2, "echo", conn.call_tool)
        result = tool.execute({"x": 1})
        self.assertEqual(result["content"][0]["text"], "hi")
        self.assertEqual(conn.calls, [("echo", {"x": 1})])

    def test_execute_returns_error_never_raises(self):
        cfg = MCPServerConfig.model_validate(_good_config())
        conn = _FakeConnection(cfg, raise_on="call_tool")
        tool = MCPTool("stub-1", "echo", 2, "echo", conn.call_tool)
        result = tool.execute({"x": 1})
        self.assertIn("error", result)
        self.assertEqual(result["server"], "stub-1")
        self.assertEqual(result["tool"], "echo")

    def test_execute_wraps_non_dict_result(self):
        tool = MCPTool("s", "t", 1, "", lambda name, args: "scalar")
        self.assertEqual(tool.execute({}), {"result": "scalar"})


class StdioIntegrationTests(unittest.TestCase):
    def test_real_subprocess_roundtrip(self):
        config = MCPServerConfig.model_validate(
            {"id": "stub", "name": "Stub", "command": sys.executable, "args": [_STUB]}
        )
        conn = StdioMCPConnection(config)
        try:
            init = conn.initialize()
            self.assertEqual(init.get("protocolVersion"), "2024-11-05")

            tools = conn.list_tools()
            self.assertEqual([t["name"] for t in tools], ["echo"])

            result = conn.call_tool("echo", {"hello": "world"})
            text = result["content"][0]["text"]
            self.assertEqual(json.loads(text), {"hello": "world"})

            # Unknown tool -> JSON-RPC error surfaced as McpError.
            with self.assertRaises(McpError):
                conn.call_tool("does-not-exist", {})
        finally:
            conn.close()

    def test_manager_connect_all_with_real_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MCPManager(Path(tmp))
            manager.install_server(
                {"id": "stub", "name": "Stub", "command": sys.executable, "args": [_STUB]}
            )
            try:
                tools = manager.connect_all()
                self.assertEqual([t.definition.tool_id for t in tools], ["mcp.stub.echo"])
                out = tools[0].execute({"a": "b"})
                self.assertEqual(json.loads(out["content"][0]["text"]), {"a": "b"})
            finally:
                manager.close_all()


if __name__ == "__main__":
    unittest.main()
