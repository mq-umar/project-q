"""Clean-room Model Context Protocol (MCP) stdio client.

Project Q can connect to owner-installed external MCP servers that speak the
PUBLIC Model Context Protocol over stdio (newline-delimited JSON-RPC 2.0). Each
remote MCP tool is wrapped as a :class:`MCPTool` conforming to the existing Tool
protocol (``definition`` + ``execute``) and namespaced under
``mcp.<server_id>.<tool_name>``. Because the tool flows through the same
ToolRegistry as every built-in tool, the EXISTING ``PolicyService.authorize_tool``
gate governs approval automatically. External MCP tools default to tier 2 so a
brand-new server needs owner confirmation in fresh contexts.

Security / robustness model (single-owner local agent):
  * NEVER HANG: a background daemon reader thread drains the subprocess stdout
    onto a queue; every request waits on that queue with a hard TIMEOUT. A
    misbehaving / silent / dead server can never block Project Q forever.
  * NEVER CRASH STARTUP: loading configs and connecting are best-effort. A
    malformed config file is skipped (never raises); a server that fails to
    connect is skipped and the rest continue.
  * NEVER RAISE FROM A TOOL: :meth:`MCPTool.execute` returns ``{"error": ...}``
    instead of propagating exceptions, mirroring the plugin Tool wrapper.
  * Injectable ``connection_factory`` lets tests drive the manager with a FAKE
    in-memory connection and NO subprocess.
  * Config ids are validated slugs; install/remove reject path traversal.

Stdlib only: subprocess, json, threading, queue, shutil, pathlib; pydantic for
the manifest.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from project_q.tools.base import ToolDefinition

# Hard caps / timeouts so nothing can run away.
MAX_MCP_CONFIG_BYTES = 64 * 1024
INIT_TIMEOUT_SECONDS = 10.0
CALL_TIMEOUT_SECONDS = 30.0
_CLOSE_WAIT_SECONDS = 3.0
_PROTOCOL_VERSION = "2024-11-05"
_MCP_ID_RE = re.compile(r"^[a-z0-9_-]{2,40}$")
# Windows reserved device names that must never become an on-disk config file.
_RESERVED_IDS = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)
_CONFIG_SUBDIR = "mcp_servers"

__all__ = [
    "MCPServerConfig",
    "McpError",
    "StdioMCPConnection",
    "MCPTool",
    "MCPManager",
    "INIT_TIMEOUT_SECONDS",
    "CALL_TIMEOUT_SECONDS",
]


class McpError(Exception):
    """Controlled error raised by a connection on timeout / dead process /
    protocol error. Always caught at the manager / tool boundary."""


class MCPServerConfig(BaseModel):
    """Declarative config for one external MCP stdio server. Unknown keys rejected."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9_-]{2,40}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    command: str = Field(min_length=1, max_length=2000)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    tier: int = Field(default=2, ge=0, le=3)
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if value.lower() in _RESERVED_IDS:
            raise ValueError("mcp server id is a reserved device name")
        return value

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must be a non-empty executable")
        return value


# ── Connection ───────────────────────────────────────────────────────────────


class StdioMCPConnection:
    """A JSON-RPC-over-stdio connection to one MCP server subprocess.

    A daemon reader thread reads stdout line-by-line and parses each line as
    JSON onto an internal queue. ``_request`` writes one JSON line to stdin and
    waits (with a TIMEOUT) for the response whose ``id`` matches, draining and
    discarding any notifications / unrelated responses in between. Nothing can
    block forever.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._next_id = 0
        self._lock = threading.Lock()
        self._closed = False

    # ── lifecycle ────────────────────────────────────────────────────────────
    def _spawn(self) -> None:
        if self._proc is not None:
            return
        env = dict(os.environ)
        # Only string->string entries; never let a config nuke the whole env.
        for key, value in (self.config.env or {}).items():
            env[str(key)] = str(value)
        argv = [self.config.command, *[str(a) for a in self.config.args]]
        # text-mode, utf-8, line buffered. stderr is logging only -> swallow it.
        self._proc = subprocess.Popen(  # noqa: S603 - owner-installed server
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._queue.put(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    # Non-JSON noise on stdout: ignore, keep reading.
                    continue
        except Exception:  # noqa: BLE001 - reader thread must die quietly
            pass
        finally:
            # Sentinel so a waiter wakes immediately when the pipe closes.
            self._queue.put(None)

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── request/response ─────────────────────────────────────────────────────
    def _send(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError("mcp server not started")
        if not self._alive():
            raise McpError("mcp server process is not running")
        try:
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise McpError(f"failed to write to mcp server: {exc}") from exc

    def _request(self, method: str, params: Optional[dict[str, Any]], timeout: float) -> dict[str, Any]:
        """Send one JSON-RPC request and wait for the response with matching id."""
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            self._send(message)

            import time as _time

            deadline = _time.monotonic() + max(0.1, float(timeout))
            while True:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    raise McpError(f"timeout waiting for response to {method}")
                try:
                    item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    raise McpError(f"timeout waiting for response to {method}")
                if item is None:
                    # reader hit EOF (process exited / pipe closed)
                    raise McpError(f"mcp server closed the connection during {method}")
                if not isinstance(item, dict):
                    continue
                # Notifications have no id -> drain & ignore.
                if "id" not in item:
                    continue
                if item.get("id") != request_id:
                    # Response for a different/older request -> ignore.
                    continue
                if "error" in item and item["error"] is not None:
                    err = item["error"]
                    if isinstance(err, dict):
                        raise McpError(str(err.get("message") or err))
                    raise McpError(str(err))
                return item.get("result") if isinstance(item.get("result"), dict) else (item.get("result") or {})

    def _notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    # ── public interface ─────────────────────────────────────────────────────
    def initialize(self) -> dict[str, Any]:
        self._spawn()
        result = self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "project-q", "version": "1.0"},
            },
            timeout=INIT_TIMEOUT_SECONDS,
        )
        # Fire-and-forget the initialized notification (no response expected).
        try:
            self._notify("notifications/initialized")
        except McpError:
            pass
        return result or {}

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {}, timeout=INIT_TIMEOUT_SECONDS)
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return []
        return [t for t in tools if isinstance(t, dict)]

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "tools/call",
            {"name": str(name), "arguments": dict(args or {})},
            timeout=CALL_TIMEOUT_SECONDS,
        )
        return result if isinstance(result, dict) else {"result": result}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=_CLOSE_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=_CLOSE_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            # Close stdout so the reader thread unblocks and no fd leaks.
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
        except Exception:  # noqa: BLE001 - close must never raise
            pass


# ── Tool wrapper ─────────────────────────────────────────────────────────────


class MCPTool:
    """A Tool-protocol wrapper around one remote MCP tool.

    ``call`` is a bound callable ``(tool_name, arguments) -> dict`` (typically
    ``connection.call_tool``). ``execute`` NEVER raises.
    """

    def __init__(
        self,
        server_id: str,
        tool_name: str,
        tier: int,
        description: str,
        call: Callable[[str, dict[str, Any]], dict[str, Any]],
        name: Optional[str] = None,
    ) -> None:
        self.server_id = server_id
        self.tool_name = tool_name
        self._call = call
        self.definition = ToolDefinition(
            tool_id=f"mcp.{server_id}.{tool_name}",
            name=name or f"{server_id}: {tool_name}",
            description=description or "",
            tier=int(tier),
        )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._call(self.tool_name, payload or {})
        except Exception as exc:  # noqa: BLE001 - a tool call must never raise
            return {"error": str(exc), "server": self.server_id, "tool": self.tool_name}
        if isinstance(result, dict):
            return result
        return {"result": result}


# ── Manager ──────────────────────────────────────────────────────────────────


class MCPManager:
    """Loads, installs, removes, lists MCP server configs and connects to them.

    ``connection_factory(config) -> connection`` defaults to
    :class:`StdioMCPConnection`; tests inject a FAKE connection (no subprocess).
    """

    def __init__(
        self,
        config_dir: Path,
        audit_service: Any = None,
        connection_factory: Optional[Callable[[MCPServerConfig], Any]] = None,
    ) -> None:
        self.config_dir = Path(config_dir) / _CONFIG_SUBDIR
        self.audit_service = audit_service
        self.connection_factory = connection_factory or StdioMCPConnection
        # Live connections keyed by server id so installs/removes are idempotent
        # and never leak (re-installing a server replaces its prior connection).
        self._connections: dict[str, Any] = {}

    # ── config IO ────────────────────────────────────────────────────────────
    def _read_config(self, path: Path) -> Optional[MCPServerConfig]:
        try:
            if path.stat().st_size > MAX_MCP_CONFIG_BYTES:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return MCPServerConfig.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError, ValueError):
            return None

    def _load_configs(self) -> list[MCPServerConfig]:
        configs: list[MCPServerConfig] = []
        try:
            if not self.config_dir.exists():
                return configs
            paths = sorted(self.config_dir.glob("*.json"))
        except OSError:
            return configs
        for path in paths:
            config = self._read_config(path)
            if config is not None:
                configs.append(config)
        return configs

    def list_servers(self) -> list[dict[str, Any]]:
        servers: list[dict[str, Any]] = []
        for config in self._load_configs():
            data = config.model_dump()
            # Redact env values (may carry tokens/secrets) from listings.
            if data.get("env"):
                data["env"] = {key: "***" for key in data["env"]}
            servers.append(data)
        return servers

    def install_server(self, config: dict[str, Any]) -> dict[str, Any]:
        """Validate + persist an MCP server config. Returns the stored config dict.

        Raises ``ValueError`` on validation failure or id collision (the server
        route maps that to HTTP 400).
        """
        if not isinstance(config, dict):
            raise ValueError("mcp server config must be a JSON object")
        raw = json.dumps(config).encode("utf-8")
        if len(raw) > MAX_MCP_CONFIG_BYTES:
            raise ValueError("mcp server config exceeds maximum size")
        try:
            parsed = MCPServerConfig.model_validate(config)
        except ValidationError as exc:
            raise ValueError(f"invalid mcp server config: {exc}") from exc

        self.config_dir.mkdir(parents=True, exist_ok=True)
        target = self.config_dir / f"{parsed.id}.json"
        if target.exists():
            raise ValueError(f"mcp server already exists: {parsed.id}")
        target.write_text(
            json.dumps(parsed.model_dump(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        if self.audit_service is not None:
            self.audit_service.log(
                action_type="mcp_install",
                action_tier=2,
                tool_name=f"mcp.{parsed.id}",
                outcome="completed",
                input_sources=["owner"],
                metadata={"id": parsed.id, "tier": parsed.tier, "command": parsed.command},
            )
        return parsed.model_dump()

    def remove_server(self, server_id: str) -> bool:
        if not _MCP_ID_RE.match(str(server_id or "")):
            return False  # reject path traversal / invalid ids
        target = self.config_dir / f"{server_id}.json"
        existed = target.exists()
        try:
            target.unlink(missing_ok=True)
        except OSError:
            existed = False
        if self.audit_service is not None:
            self.audit_service.log(
                action_type="mcp_remove",
                action_tier=2,
                tool_name=f"mcp.{server_id}",
                outcome="completed" if existed else "skipped",
                input_sources=["owner"],
                metadata={"id": server_id},
            )
        return existed

    # ── connect ──────────────────────────────────────────────────────────────
    def connect_server(self, config: MCPServerConfig) -> list[MCPTool]:
        """Connect ONE enabled server (closing+replacing any prior connection for
        the same id) and return its MCPTools. Best-effort: never raises."""
        if not config.enabled:
            return []
        # Replace any existing connection for this id so re-installs don't leak.
        self.disconnect_server(config.id)
        connection = None
        try:
            connection = self.connection_factory(config)
            connection.initialize()
            remote_tools = connection.list_tools()
        except Exception:  # noqa: BLE001 - one bad server must not break others
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001
                    pass
            return []
        self._connections[config.id] = connection
        tools: list[MCPTool] = []
        for remote in remote_tools or []:
            if not isinstance(remote, dict):
                continue
            tool_name = str(remote.get("name") or "").strip()
            if not tool_name:
                continue
            tools.append(
                MCPTool(
                    server_id=config.id,
                    tool_name=tool_name,
                    tier=config.tier,
                    description=str(remote.get("description") or ""),
                    call=connection.call_tool,
                )
            )
        return tools

    def connect_all(self) -> list[MCPTool]:
        """Best-effort connect to every enabled server (idempotent per id) and
        build MCPTools. SKIP+continue on any error (never raise)."""
        tools: list[MCPTool] = []
        for config in self._load_configs():
            tools.extend(self.connect_server(config))
        return tools

    def disconnect_server(self, server_id: str) -> None:
        """Close + drop the live connection for one server (if any)."""
        connection = self._connections.pop(str(server_id), None)
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    def close_all(self) -> None:
        for connection in list(self._connections.values()):
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
        self._connections = {}
