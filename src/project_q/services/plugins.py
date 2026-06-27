"""Declarative, owner-installable, tier-gated plugin/skill system.

A plugin is a JSON manifest (NEVER Python) that declares either an HTTP request
or a shell command. Each manifest becomes a :class:`PluginTool` that conforms to
the existing Tool protocol (``definition`` + ``execute``) and is namespaced under
``plugin.<id>``. Because the tool flows through the same ToolRegistry as every
built-in tool, the EXISTING ``PolicyService.authorize_tool`` gate (invoked by both
``/api/tools/execute`` and ``PlanExecutorService``) governs approval automatically.

Security model (single-owner local agent):
  * No arbitrary code: a manifest can only describe a declarative HTTP request or
    a shell command string. There is no import path, no eval/exec, no callable
    reference. ``extra='forbid'`` rejects smuggled keys. ``input_schema`` is
    advisory and never executed.
  * Templating is pure string substitution (``format_map`` with a default-empty
    mapping) from the execute payload — it cannot reach attributes/builtins and
    never raises on a missing key.
  * Shell plugins are FORCED to tier 3 (owner approval every run). HTTP plugins
    may self-declare tier 0..2 only and can never self-escalate to tier 3.
  * Loading is best-effort: a malformed/oversized/hostile file is skipped and can
    never break built-in tool registration or app startup.

Stdlib only: json, os, subprocess, urllib, pathlib.
"""
from __future__ import annotations

import json
import os
import re
import string
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from project_q.tools.base import ToolDefinition

# Hard caps to bound resource usage / audit growth.
MAX_PLUGIN_BYTES = 64 * 1024
_HTTP_TIMEOUT_SECONDS = 15
_SHELL_TIMEOUT_SECONDS = 30
_MAX_BODY_BYTES = 8 * 1024
_MAX_STREAM_CHARS = 8 * 1024
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "[::1]"})
_PLUGIN_ID_RE = re.compile(r"^[a-z0-9_-]{2,40}$")
# Windows reserved device names that must never become an on-disk manifest file.
_RESERVED_IDS = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects so an allowed (e.g. loopback) URL cannot be
    bounced to an internal host — closes the SSRF-via-redirect bypass."""

    def redirect_request(self, *args: Any, **kwargs: Any):  # noqa: D401
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)

__all__ = [
    "PluginManifest",
    "PluginTool",
    "PluginManager",
    "MAX_PLUGIN_BYTES",
]


class _FlatFormatter(string.Formatter):
    """Flat ``{key}`` substitution only.

    Rejects attribute/index traversal (``{x.__class__}``, ``{x[0]}``) so a
    template can never read object internals into an outbound request, and never
    raises (missing key -> '').
    """

    def get_field(self, field_name: str, args: Any, kwargs: Any) -> tuple[Any, str]:
        if any(ch in field_name for ch in ".[]"):
            return "", field_name
        return kwargs.get(field_name, ""), field_name

    def convert_field(self, value: Any, conversion: Any) -> Any:
        return value

    def format_field(self, value: Any, format_spec: str) -> str:
        return str(value)


_FLAT_FORMATTER = _FlatFormatter()


def _safe_format(template: str, payload: dict[str, Any]) -> str:
    """Pure flat-key substitution; never raises and never traverses attributes."""
    mapping = {str(k): ("" if v is None else str(v)) for k, v in (payload or {}).items()}
    try:
        return _FLAT_FORMATTER.vformat(str(template), (), mapping)
    except Exception:  # noqa: BLE001 - templating must never raise
        return str(template)


def _strip_crlf(value: str) -> str:
    """Defend against CRLF header injection from attacker-controlled payloads."""
    return str(value).replace("\r", "").replace("\n", "")


class PluginManifest(BaseModel):
    """Declarative plugin manifest. Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9_-]{2,40}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    tier: int = Field(default=1, ge=0, le=3)
    type: Literal["http", "shell"]
    input_schema: dict[str, Any] = Field(default_factory=dict)

    # HTTP-only fields
    method: Literal["GET", "POST"] = "GET"
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    body_template: str = ""

    # Shell-only fields
    command: str = ""

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if value.lower() in _RESERVED_IDS:
            raise ValueError("plugin id is a reserved device name")
        return value

    @field_validator("url")
    @classmethod
    def _validate_url_scheme(cls, value: str) -> str:
        if not value:
            return value
        scheme = urlparse(value).scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError("url scheme must be http or https")
        return value

    @model_validator(mode="after")
    def _validate_by_type(self) -> "PluginManifest":
        if self.type == "http":
            # HTTP plugins may NOT self-declare tier 3 (owner-approval-only is
            # reserved for shell). Reject rather than silently clamp so a hostile
            # manifest is surfaced.
            if int(self.tier) >= 3:
                raise ValueError("http plugins may not self-declare tier 3")
            if not self.url:
                raise ValueError("http plugins require a url")
            # Re-assert scheme (covers url provided without the field validator path).
            parsed = urlparse(self.url)
            scheme = parsed.scheme.lower()
            if scheme not in {"http", "https"}:
                raise ValueError("url scheme must be http or https")
            # SSRF defense: the destination HOST must be a literal fixed at install
            # time. Only the path/query may be payload-templated — a templated host
            # ('{...}' in the netloc) would let an execute-time payload retarget the
            # request to an arbitrary internal/cloud-metadata host.
            if not parsed.hostname or "{" in (parsed.netloc or ""):
                raise ValueError("http plugin url host must be a literal (no host templating)")
            # Clamp tier into 0..2 (never 3 by self-declaration).
            object.__setattr__(self, "tier", max(0, min(int(self.tier), 2)))
        elif self.type == "shell":
            if not self.command.strip():
                raise ValueError("shell plugins require a non-empty command")
            # Shell is ALWAYS owner-approval-every-run.
            object.__setattr__(self, "tier", 3)
        return self


class PluginTool:
    """A Tool-protocol wrapper around a declarative :class:`PluginManifest`."""

    def __init__(
        self,
        manifest: PluginManifest,
        workspace_root: Path | None = None,
        settings_service: Any = None,
        audit_service: Any = None,
    ) -> None:
        self.manifest = manifest
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.settings_service = settings_service
        self.audit_service = audit_service
        self.definition = ToolDefinition(
            tool_id=f"plugin.{manifest.id}",
            name=manifest.name,
            description=manifest.description,
            tier=manifest.tier,
        )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = payload or {}
        if self.manifest.type == "http":
            return self._execute_http(payload)
        return self._execute_shell(payload)

    # ── HTTP ────────────────────────────────────────────────────────────────
    def _execute_http(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = _safe_format(self.manifest.url, payload)
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return {"error": "url scheme must be http or https"}

        if self._network_local_only() and not self._is_loopback(parsed.hostname):
            return {"error": "network_policy=local_only blocks non-loopback url"}

        headers = {
            _strip_crlf(k): _strip_crlf(_safe_format(str(v), payload))
            for k, v in self.manifest.headers.items()
        }
        data: bytes | None = None
        if self.manifest.method == "POST" and self.manifest.body_template:
            body = _safe_format(self.manifest.body_template, payload)
            data = body.encode("utf-8")[:_MAX_BODY_BYTES]

        request = urllib.request.Request(
            url,
            data=data,
            method=self.manifest.method,
            headers=headers,
        )
        try:
            with _NO_REDIRECT_OPENER.open(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read(_MAX_BODY_BYTES + 1)
                status_code = int(getattr(response, "status", 0) or response.getcode() or 0)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(_MAX_BODY_BYTES + 1)
            except Exception:  # noqa: BLE001
                raw = b""
            status_code = int(exc.code)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"error": str(exc), "url": url}

        body_text = raw.decode("utf-8", errors="replace")
        truncated = len(body_text) > _MAX_BODY_BYTES
        return {
            "status_code": status_code,
            "body": body_text[:_MAX_BODY_BYTES],
            "truncated": truncated,
            "url": url,
        }

    def _network_local_only(self) -> bool:
        if self.settings_service is None:
            return False
        try:
            settings = self.settings_service.get_all()
        except Exception:  # noqa: BLE001
            return False
        return str(settings.get("network_policy", "selected_services")) == "local_only"

    @staticmethod
    def _is_loopback(hostname: str | None) -> bool:
        if not hostname:
            return False
        return hostname.strip().lower() in _LOOPBACK_HOSTS

    # ── SHELL ───────────────────────────────────────────────────────────────
    def _execute_shell(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = _safe_format(self.manifest.command, payload)
        if sys.platform == "win32":
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            argv = ["sh", "-c", command]
        cwd = str(self.workspace_root) if self.workspace_root else None
        try:
            completed = subprocess.run(  # noqa: S603 - tier-3 owner-approved
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=_SHELL_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"error": f"command timed out after {_SHELL_TIMEOUT_SECONDS}s"}
        except (OSError, ValueError) as exc:
            return {"error": str(exc)}
        return {
            "returncode": completed.returncode,
            "stdout": (completed.stdout or "")[:_MAX_STREAM_CHARS],
            "stderr": (completed.stderr or "")[:_MAX_STREAM_CHARS],
        }


class PluginManager:
    """Loads, installs, removes, and lists declarative plugin manifests."""

    def __init__(
        self,
        plugins_dir: Path,
        audit_service: Any = None,
        workspace_root: Path | None = None,
        settings_service: Any = None,
        builtin_tool_ids: set[str] | None = None,
    ) -> None:
        self.plugins_dir = Path(plugins_dir)
        self.audit_service = audit_service
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.settings_service = settings_service
        self.builtin_tool_ids = set(builtin_tool_ids or set())

    # ── Loading ─────────────────────────────────────────────────────────────
    def load_all(self) -> list[PluginTool]:
        """Read every valid manifest; SKIP+continue on any error. Never raises."""
        tools: list[PluginTool] = []
        try:
            if not self.plugins_dir.exists():
                return tools
            paths = sorted(self.plugins_dir.glob("*.json"))
        except OSError:
            return tools
        for path in paths:
            manifest = self._read_manifest(path)
            if manifest is None:
                continue
            tools.append(self._build_tool(manifest))
        return tools

    def _read_manifest(self, path: Path) -> PluginManifest | None:
        try:
            if path.stat().st_size > MAX_PLUGIN_BYTES:
                return None
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return PluginManifest.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError, ValueError):
            return None

    def _build_tool(self, manifest: PluginManifest) -> PluginTool:
        return PluginTool(
            manifest,
            workspace_root=self.workspace_root,
            settings_service=self.settings_service,
            audit_service=self.audit_service,
        )

    # ── Install / Remove / List ─────────────────────────────────────────────
    def install(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Validate + persist a manifest. Returns the new tool definition dict.

        Raises ``ValueError`` on validation failure or id collision (the server
        route maps that to HTTP 400).
        """
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be a JSON object")
        raw = json.dumps(manifest).encode("utf-8")
        if len(raw) > MAX_PLUGIN_BYTES:
            raise ValueError("manifest exceeds maximum size")
        try:
            parsed = PluginManifest.model_validate(manifest)
        except ValidationError as exc:
            raise ValueError(f"invalid plugin manifest: {exc}") from exc

        tool_id = f"plugin.{parsed.id}"
        if parsed.id in self.builtin_tool_ids or tool_id in self.builtin_tool_ids:
            raise ValueError(f"plugin id collides with a built-in tool: {parsed.id}")
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        target = self.plugins_dir / f"{parsed.id}.json"
        if target.exists():
            raise ValueError(f"plugin already exists: {parsed.id}")

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
                action_type="plugin_install",
                action_tier=2,
                tool_name=tool_id,
                outcome="completed",
                input_sources=["owner"],
                metadata={"id": parsed.id, "type": parsed.type, "tier": parsed.tier},
            )

        tool = self._build_tool(parsed)
        return {
            "tool_id": tool.definition.tool_id,
            "name": tool.definition.name,
            "description": tool.definition.description,
            "tier": tool.definition.tier,
        }

    def build_tool(self, manifest: dict[str, Any]) -> PluginTool:
        """Build a PluginTool from an already-validated manifest dict (no I/O)."""
        parsed = PluginManifest.model_validate(manifest)
        return self._build_tool(parsed)

    def get_tool(self, plugin_id: str) -> PluginTool | None:
        """Return the PluginTool for an installed plugin id, or None."""
        if not _PLUGIN_ID_RE.match(str(plugin_id or "")):
            return None
        target = self.plugins_dir / f"{plugin_id}.json"
        manifest = self._read_manifest(target)
        if manifest is None:
            return None
        return self._build_tool(manifest)

    def remove(self, plugin_id: str) -> bool:
        if not _PLUGIN_ID_RE.match(str(plugin_id or "")):
            return False  # reject path traversal / invalid ids
        target = self.plugins_dir / f"{plugin_id}.json"
        existed = target.exists()
        try:
            target.unlink(missing_ok=True)
        except OSError:
            existed = False
        if self.audit_service is not None:
            self.audit_service.log(
                action_type="plugin_remove",
                action_tier=2,
                tool_name=f"plugin.{plugin_id}",
                outcome="completed" if existed else "skipped",
                input_sources=["owner"],
                metadata={"id": plugin_id},
            )
        return existed

    def list(self) -> list[dict[str, Any]]:
        manifests: list[dict[str, Any]] = []
        try:
            if not self.plugins_dir.exists():
                return manifests
            paths = sorted(self.plugins_dir.glob("*.json"))
        except OSError:
            return manifests
        for path in paths:
            manifest = self._read_manifest(path)
            if manifest is None:
                continue
            data = manifest.model_dump()
            # Redact header values (may carry tokens) from listings.
            if data.get("headers"):
                data["headers"] = {key: "***" for key in data["headers"]}
            manifests.append(data)
        return manifests
