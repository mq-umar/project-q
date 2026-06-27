"""Bearer-token connector framework + GitHub and Slack connector tools.

A connector is a Tool-protocol class that performs a bounded, redirect-refusing
HTTPS request to a fixed third-party API host, authenticating with a
``Authorization: Bearer <token>`` header whose token is resolved at execute()
time from the DPAPI-sealed :class:`VaultService` by a fixed secret name. No token
is ever hardcoded; a missing secret yields ``{"error": ...}`` with NO network
call, so the whole framework is testable without live credentials.

Security model (single-owner local agent):
  * Tokens resolved at execute() from the vault by fixed names (github_token,
    slack_bot_token). Absent secret -> {error} and no transport call.
  * SSRF-via-redirect is closed by a _NoRedirectHandler opener (mirrors
    services/plugins.py): redirect_request returns None.
  * Base URLs are fixed https constants; payload only fills path segments / body,
    never the scheme or host, so a payload cannot retarget an internal host.
  * All header values are CRLF-stripped to block header injection.
  * Response reads are byte-bounded to prevent memory blowup.
  * execute() NEVER raises — every error path returns {error}.
  * network_policy=local_only is enforced in-tool (execute-time gate) in addition
    to the PolicyService._INTERNET_TOOLS dispatch gate.

Stdlib only: json, urllib.request, urllib.error.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from project_q.tools.base import ToolDefinition

# Injectable transport contract:
#   (url, method, headers, body_bytes, timeout) -> dict
Transport = Callable[[str, str, dict, "bytes | None", float], dict]

_HTTP_TIMEOUT_SECONDS = 15
# Bounded body: a realistic API ceiling so normal (large) connector responses are
# not silently truncated into a JSON-parse error, while still capping memory.
_MAX_BODY_BYTES = 4 * 1024 * 1024

_GITHUB_BASE = "https://api.github.com"
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}
_SLACK_BASE = "https://slack.com/api"

_GITHUB_SECRET = "github_token"
_SLACK_SECRET = "slack_bot_token"
_NOTION_BASE = "https://api.notion.com/v1"
_NOTION_HEADERS = {"Notion-Version": "2022-06-28"}
_NOTION_SECRET = "notion_token"
_TODOIST_BASE = "https://api.todoist.com/rest/v2"
_TODOIST_SECRET = "todoist_token"
_LINEAR_BASE = "https://api.linear.app"
_LINEAR_SECRET = "linear_api_key"
# ── Google OAuth2 (refresh-token grant) ───────────────────────────────────────
# Unlike the other connectors (which present a single long-lived bearer token
# straight from the vault), Google APIs require a short-lived access token minted
# at execute() time by exchanging a stored refresh token at the token endpoint.
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_CLIENT_ID_SECRET = "google_client_id"
_GOOGLE_CLIENT_SECRET_SECRET = "google_client_secret"
_GOOGLE_REFRESH_TOKEN_SECRET = "google_refresh_token"
_GCAL_BASE = "https://www.googleapis.com/calendar/v3"
_GDRIVE_BASE = "https://www.googleapis.com/drive/v3"
# ── Microsoft Graph OAuth2 (refresh-token grant) ──────────────────────────────
_MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_MS_CLIENT_ID_SECRET = "ms_client_id"
_MS_CLIENT_SECRET_SECRET = "ms_client_secret"
_MS_REFRESH_TOKEN_SECRET = "ms_refresh_token"
_MS_SCOPE = "https://graph.microsoft.com/.default offline_access"
_MSGRAPH_BASE = "https://graph.microsoft.com/v1.0"
# A GitHub repo must be exactly owner/name — reject extra path/query/CRLF so an
# owner-supplied value cannot alter the request line (confused-deputy hardening).
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
# Bare id segment for Notion/Todoist path interpolation (no slashes/query/CRLF).
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

__all__ = [
    "GitHubListReposTool",
    "GitHubListIssuesTool",
    "GitHubCreateIssueTool",
    "SlackListChannelsTool",
    "SlackPostMessageTool",
    "NotionSearchTool",
    "NotionCreatePageTool",
    "TodoistListTasksTool",
    "TodoistCreateTaskTool",
    "LinearListIssuesTool",
    "LinearCreateIssueTool",
    "GoogleCalendarListEventsTool",
    "GoogleCalendarCreateEventTool",
    "GoogleDriveListFilesTool",
    "MicrosoftListMailTool",
    "MicrosoftListEventsTool",
    "MicrosoftListDriveTool",
    "MicrosoftSendMailTool",
]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects so a fixed https host cannot be bounced to an
    internal host — closes the SSRF-via-redirect bypass (mirrors plugins.py)."""

    def redirect_request(self, *args: Any, **kwargs: Any):  # noqa: D401
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _strip_crlf(value: str) -> str:
    """Defend against CRLF header injection from attacker-controlled payloads."""
    return str(value).replace("\r", "").replace("\n", "")


def _default_transport(
    url: str,
    method: str,
    headers: dict,
    body_bytes: bytes | None,
    timeout: float,
) -> dict:
    """Real transport: urllib.request via the no-redirect opener, bounded read.

    Returns the parsed JSON dict, or {error, status_code?} on any failure. Must
    not raise — _BearerHttpClient also wraps the call, but keep this defensive.
    """
    request = urllib.request.Request(url, data=body_bytes, method=method, headers=headers)
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(_MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(_MAX_BODY_BYTES)
        except Exception:  # noqa: BLE001
            raw = b""
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            parsed = {"body": raw.decode("utf-8", errors="replace")}
        if isinstance(parsed, dict):
            parsed.setdefault("status_code", int(exc.code))
            return parsed
        return {"error": "non-object response", "status_code": int(exc.code)}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"error": str(exc)}
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        # 2xx with an empty body (e.g. send-mail 202, close-task 204) — success.
        return {"status": "ok"}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return {"error": f"invalid JSON response: {exc}"}


class _BearerHttpClient:
    """Performs token-authenticated GET/POST to a fixed https API host."""

    def __init__(
        self,
        token: str,
        base_url: str,
        default_headers: dict | None = None,
        timeout: float = _HTTP_TIMEOUT_SECONDS,
        transport: Transport | None = None,
        auth_scheme: str = "bearer",
    ) -> None:
        self.token = token
        self.base_url = base_url
        self.default_headers = dict(default_headers or {})
        self.timeout = timeout
        self.transport = transport or _default_transport
        # "bearer" -> "Authorization: Bearer <token>"; "raw" -> "Authorization: <token>"
        # (Linear and some APIs expect the bare key with no scheme prefix).
        self.auth_scheme = auth_scheme

    def request(self, method: str, path: str, body: Any = None) -> dict:
        """Build and send the request; NEVER raises — errors -> {error}."""
        try:
            url = self.base_url + path
            auth_value = self.token if self.auth_scheme == "raw" else f"Bearer {self.token}"
            headers = {"Authorization": auth_value}
            headers.update(self.default_headers)
            body_bytes: bytes | None = None
            if method == "POST":
                headers["Content-Type"] = "application/json"
                if body is not None:
                    body_bytes = json.dumps(body).encode("utf-8")
            # CRLF-strip every header value to block header injection.
            headers = {_strip_crlf(k): _strip_crlf(v) for k, v in headers.items()}
            result = self.transport(url, method, headers, body_bytes, self.timeout)
            if isinstance(result, dict):
                return result
            return {"error": "transport returned a non-dict response"}
        except Exception as exc:  # noqa: BLE001 - execute() must never raise
            return {"error": str(exc)}


class _ConnectorBase:
    """Mixin providing the local_only gate + vault token resolution."""

    def __init__(self, vault: Any, settings_service: Any = None, transport: Transport | None = None) -> None:
        self.vault = vault
        self.settings_service = settings_service
        self.transport = transport

    def _network_local_only(self) -> bool:
        if self.settings_service is None:
            return False
        try:
            settings = self.settings_service.get_all()
        except Exception:  # noqa: BLE001
            return False
        return str(settings.get("network_policy", "selected_services")) == "local_only"

    def _resolve_token(self, secret_name: str) -> tuple[str | None, dict | None]:
        """Return (token, None) or (None, {error}) if the secret is absent."""
        try:
            return self.vault.get_secret(secret_name), None
        except KeyError:
            return None, {"error": f"{secret_name} not configured in the vault"}
        except Exception as exc:  # noqa: BLE001 - never raise out of execute()
            return None, {"error": str(exc)}


# ── GitHub connectors ───────────────────────────────────────────────────────
class GitHubListReposTool(_ConnectorBase):
    definition = ToolDefinition(
        tool_id="github.list_repos",
        name="GitHub: List Repositories",
        description="List the authenticated user's GitHub repositories.",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks github.list_repos"}
        token, err = self._resolve_token(_GITHUB_SECRET)
        if err is not None:
            return err
        client = _BearerHttpClient(token, _GITHUB_BASE, _GITHUB_HEADERS, transport=self.transport)
        return client.request("GET", "/user/repos")


class GitHubListIssuesTool(_ConnectorBase):
    definition = ToolDefinition(
        tool_id="github.list_issues",
        name="GitHub: List Issues",
        description="List issues for a repository. payload {repo:'owner/name'}.",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks github.list_issues"}
        payload = payload or {}
        repo = str(payload.get("repo", "")).strip()
        if not _REPO_RE.match(repo):
            return {"error": "invalid repo (expected owner/name)"}
        token, err = self._resolve_token(_GITHUB_SECRET)
        if err is not None:
            return err
        client = _BearerHttpClient(token, _GITHUB_BASE, _GITHUB_HEADERS, transport=self.transport)
        return client.request("GET", f"/repos/{repo}/issues")


class GitHubCreateIssueTool(_ConnectorBase):
    definition = ToolDefinition(
        tool_id="github.create_issue",
        name="GitHub: Create Issue",
        description="Create an issue. payload {repo:'owner/name', title, body}.",
        # External write (creates owner-attributed public content): tier 2 so it
        # needs confirmation under the default auto_approve_tier=1, matching
        # slack.post_message rather than auto-firing from an automated plan.
        tier=2,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks github.create_issue"}
        payload = payload or {}
        repo = str(payload.get("repo", "")).strip()
        if not _REPO_RE.match(repo):
            return {"error": "invalid repo (expected owner/name)"}
        token, err = self._resolve_token(_GITHUB_SECRET)
        if err is not None:
            return err
        body = {"title": payload.get("title", ""), "body": payload.get("body", "")}
        client = _BearerHttpClient(token, _GITHUB_BASE, _GITHUB_HEADERS, transport=self.transport)
        return client.request("POST", f"/repos/{repo}/issues", body)


# ── Slack connectors ────────────────────────────────────────────────────────
class SlackListChannelsTool(_ConnectorBase):
    definition = ToolDefinition(
        tool_id="slack.list_channels",
        name="Slack: List Channels",
        description="List Slack conversations (channels).",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks slack.list_channels"}
        token, err = self._resolve_token(_SLACK_SECRET)
        if err is not None:
            return err
        client = _BearerHttpClient(token, _SLACK_BASE, transport=self.transport)
        return client.request("GET", "/conversations.list")


class SlackPostMessageTool(_ConnectorBase):
    definition = ToolDefinition(
        tool_id="slack.post_message",
        name="Slack: Post Message",
        description="Post a message to a channel. payload {channel, text}.",
        tier=2,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks slack.post_message"}
        payload = payload or {}
        channel = str(payload.get("channel", "")).strip()
        if not channel:
            return {"error": "channel is required"}
        token, err = self._resolve_token(_SLACK_SECRET)
        if err is not None:
            return err
        body = {"channel": channel, "text": payload.get("text", "")}
        client = _BearerHttpClient(token, _SLACK_BASE, transport=self.transport)
        return client.request("POST", "/chat.postMessage", body)


# ── Notion connectors ─────────────────────────────────────────────────────────
class NotionSearchTool(_ConnectorBase):
    definition = ToolDefinition(
        tool_id="notion.search",
        name="Notion: Search",
        description="Search Notion pages/databases. payload {query}.",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks notion.search"}
        payload = payload or {}
        token, err = self._resolve_token(_NOTION_SECRET)
        if err is not None:
            return err
        client = _BearerHttpClient(token, _NOTION_BASE, _NOTION_HEADERS, transport=self.transport)
        return client.request("POST", "/search", {"query": str(payload.get("query", ""))})


class NotionCreatePageTool(_ConnectorBase):
    definition = ToolDefinition(
        tool_id="notion.create_page",
        # Writes to the owner's own private workspace (PRD Notion Write = Tier 1).
        name="Notion: Create Page",
        description="Create a page under a parent. payload {parent_page_id, title}.",
        tier=1,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks notion.create_page"}
        payload = payload or {}
        parent = str(payload.get("parent_page_id", "")).strip()
        if not _ID_RE.match(parent):
            return {"error": "valid parent_page_id is required"}
        token, err = self._resolve_token(_NOTION_SECRET)
        if err is not None:
            return err
        body = {
            "parent": {"page_id": parent},
            "properties": {
                "title": {"title": [{"text": {"content": str(payload.get("title", ""))}}]}
            },
        }
        client = _BearerHttpClient(token, _NOTION_BASE, _NOTION_HEADERS, transport=self.transport)
        return client.request("POST", "/pages", body)


# ── Todoist connectors ────────────────────────────────────────────────────────
class TodoistListTasksTool(_ConnectorBase):
    definition = ToolDefinition(
        tool_id="todoist.list_tasks",
        name="Todoist: List Tasks",
        description="List active Todoist tasks.",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks todoist.list_tasks"}
        token, err = self._resolve_token(_TODOIST_SECRET)
        if err is not None:
            return err
        client = _BearerHttpClient(token, _TODOIST_BASE, transport=self.transport)
        return client.request("GET", "/tasks")


class TodoistCreateTaskTool(_ConnectorBase):
    definition = ToolDefinition(
        tool_id="todoist.create_task",
        name="Todoist: Create Task",
        description="Create a task. payload {content, due_string?, priority?}.",
        tier=1,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks todoist.create_task"}
        payload = payload or {}
        content = str(payload.get("content", "")).strip()
        if not content:
            return {"error": "content is required"}
        body: dict[str, Any] = {"content": content}
        if payload.get("due_string"):
            body["due_string"] = str(payload["due_string"])
        if isinstance(payload.get("priority"), int):
            body["priority"] = payload["priority"]
        token, err = self._resolve_token(_TODOIST_SECRET)
        if err is not None:
            return err
        client = _BearerHttpClient(token, _TODOIST_BASE, transport=self.transport)
        return client.request("POST", "/tasks", body)


# ── Linear connectors (GraphQL, raw-key auth) ─────────────────────────────────
class LinearListIssuesTool(_ConnectorBase):
    definition = ToolDefinition(
        tool_id="linear.list_issues",
        name="Linear: List Issues",
        description="List recent Linear issues.",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks linear.list_issues"}
        token, err = self._resolve_token(_LINEAR_SECRET)
        if err is not None:
            return err
        query = "{ issues(first: 20) { nodes { id title state { name } } } }"
        client = _BearerHttpClient(token, _LINEAR_BASE, transport=self.transport, auth_scheme="raw")
        return client.request("POST", "/graphql", {"query": query})


class LinearCreateIssueTool(_ConnectorBase):
    definition = ToolDefinition(
        tool_id="linear.create_issue",
        name="Linear: Create Issue",
        # External write visible to a shared Linear team: tier 2 for parity with
        # github.create_issue / slack.post_message / msgraph.send_mail.
        description="Create an issue. payload {team_id, title, description?}.",
        tier=2,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks linear.create_issue"}
        payload = payload or {}
        team_id = str(payload.get("team_id", "")).strip()
        if not _ID_RE.match(team_id):
            return {"error": "valid team_id is required"}
        title = str(payload.get("title", "")).strip()
        if not title:
            return {"error": "title is required"}
        token, err = self._resolve_token(_LINEAR_SECRET)
        if err is not None:
            return err
        mutation = (
            "mutation IssueCreate($title: String!, $teamId: String!, $description: String) "
            "{ issueCreate(input: {title: $title, teamId: $teamId, description: $description}) "
            "{ success issue { id identifier } } }"
        )
        variables = {
            "title": title,
            "teamId": team_id,
            "description": str(payload.get("description", "")),
        }
        client = _BearerHttpClient(token, _LINEAR_BASE, transport=self.transport, auth_scheme="raw")
        return client.request("POST", "/graphql", {"query": mutation, "variables": variables})


# ── Google OAuth2 connectors (refresh-token grant -> short-lived access token) ─
class _GoogleOAuthBase(_ConnectorBase):
    """Mixin that mints a short-lived Google access token from a stored refresh
    token before each API call.

    Google's three secrets (client id, client secret, refresh token) live in the
    vault. The client_secret, refresh_token, and the minted access_token are
    OAuth bearer credentials: they must NEVER appear in any returned dict, audit
    record, or error string. Every failure therefore collapses to a fixed,
    secret-free message. The exchange reuses self.transport (injectable) and the
    no-redirect opener via _BearerHttpClient/_default_transport, so the same SSRF
    and header-injection defenses as the other connectors apply, and the layer is
    fully testable without live Google credentials.
    """

    def _google_access_token(self) -> tuple[str | None, dict | None]:
        """Return (access_token, None) or (None, {error}). NEVER raises.

        If ANY of the three secrets is absent, return the not-configured error
        with NO network call (graceful no-creds behavior). Otherwise POST a
        form-urlencoded refresh_token grant to the token endpoint and parse the
        access_token; any failure -> a generic, secret-free error.
        """
        try:
            client_id = self.vault.get_secret(_GOOGLE_CLIENT_ID_SECRET)
            client_secret = self.vault.get_secret(_GOOGLE_CLIENT_SECRET_SECRET)
            refresh_token = self.vault.get_secret(_GOOGLE_REFRESH_TOKEN_SECRET)
        except KeyError:
            return None, {
                "error": (
                    "google OAuth not configured in the vault (need "
                    "google_client_id, google_client_secret, google_refresh_token)"
                )
            }
        except Exception:  # noqa: BLE001 - never raise out of execute()
            return None, {"error": "google token refresh failed"}

        try:
            form = urllib.parse.urlencode(
                {
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                }
            ).encode("utf-8")
            headers = {
                _strip_crlf("Content-Type"): _strip_crlf(
                    "application/x-www-form-urlencoded"
                )
            }
            transport = self.transport or _default_transport
            result = transport(
                _GOOGLE_TOKEN_URL, "POST", headers, form, _HTTP_TIMEOUT_SECONDS
            )
            if isinstance(result, dict):
                access_token = result.get("access_token")
                if isinstance(access_token, str) and access_token:
                    return access_token, None
            return None, {"error": "google token refresh failed"}
        except Exception:  # noqa: BLE001 - never leak an exception carrying a secret
            return None, {"error": "google token refresh failed"}


class GoogleCalendarListEventsTool(_GoogleOAuthBase):
    definition = ToolDefinition(
        tool_id="google_calendar.list_events",
        name="Google Calendar: List Events",
        description="List upcoming events on the primary Google Calendar.",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks google_calendar.list_events"}
        access_token, err = self._google_access_token()
        if err is not None:
            return err
        client = _BearerHttpClient(access_token, _GCAL_BASE, transport=self.transport)
        return client.request("GET", "/calendars/primary/events")


class GoogleCalendarCreateEventTool(_GoogleOAuthBase):
    definition = ToolDefinition(
        tool_id="google_calendar.create_event",
        # Writes to the owner's own primary calendar (mirrors notion.create_page /
        # todoist.create_task private-write tier 1).
        name="Google Calendar: Create Event",
        description="Create an event. payload {summary, start, end} (RFC3339 dateTimes).",
        tier=1,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks google_calendar.create_event"}
        payload = payload or {}
        summary = str(payload.get("summary", "")).strip()
        if not summary:
            return {"error": "summary is required"}
        start = str(payload.get("start", "")).strip()
        end = str(payload.get("end", "")).strip()
        if not start or not end:
            return {"error": "start and end are required"}
        access_token, err = self._google_access_token()
        if err is not None:
            return err
        body = {
            "summary": summary,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        client = _BearerHttpClient(access_token, _GCAL_BASE, transport=self.transport)
        return client.request("POST", "/calendars/primary/events", body)


class GoogleDriveListFilesTool(_GoogleOAuthBase):
    definition = ToolDefinition(
        tool_id="google_drive.list_files",
        name="Google Drive: List Files",
        description="List files in the owner's Google Drive.",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks google_drive.list_files"}
        access_token, err = self._google_access_token()
        if err is not None:
            return err
        client = _BearerHttpClient(access_token, _GDRIVE_BASE, transport=self.transport)
        return client.request("GET", "/files")


# ── Microsoft Graph connectors (OAuth2 refresh-token) ─────────────────────────
class _MicrosoftOAuthBase(_ConnectorBase):
    """Mints a short-lived Microsoft Graph access token from a stored refresh
    token. Mirrors _GoogleOAuthBase: client_secret / refresh_token / access_token
    NEVER appear in any returned dict or error string; missing secrets -> {error}
    with no network call; failures collapse to a fixed secret-free message."""

    def _ms_access_token(self) -> tuple[str | None, dict | None]:
        try:
            client_id = self.vault.get_secret(_MS_CLIENT_ID_SECRET)
            client_secret = self.vault.get_secret(_MS_CLIENT_SECRET_SECRET)
            refresh_token = self.vault.get_secret(_MS_REFRESH_TOKEN_SECRET)
        except KeyError:
            return None, {
                "error": (
                    "microsoft OAuth not configured in the vault (need "
                    "ms_client_id, ms_client_secret, ms_refresh_token)"
                )
            }
        except Exception:  # noqa: BLE001 - never raise out of execute()
            return None, {"error": "microsoft token refresh failed"}
        try:
            form = urllib.parse.urlencode(
                {
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "scope": _MS_SCOPE,
                }
            ).encode("utf-8")
            headers = {_strip_crlf("Content-Type"): _strip_crlf("application/x-www-form-urlencoded")}
            transport = self.transport or _default_transport
            result = transport(_MS_TOKEN_URL, "POST", headers, form, _HTTP_TIMEOUT_SECONDS)
            if isinstance(result, dict):
                access_token = result.get("access_token")
                if isinstance(access_token, str) and access_token:
                    return access_token, None
            return None, {"error": "microsoft token refresh failed"}
        except Exception:  # noqa: BLE001 - never leak an exception carrying a secret
            return None, {"error": "microsoft token refresh failed"}

    def _graph(self, access_token: str) -> "_BearerHttpClient":
        return _BearerHttpClient(access_token, _MSGRAPH_BASE, transport=self.transport)


class MicrosoftListMailTool(_MicrosoftOAuthBase):
    definition = ToolDefinition(
        tool_id="msgraph.list_mail",
        name="Microsoft Graph: List Mail",
        description="List recent Outlook messages.",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks msgraph.list_mail"}
        access_token, err = self._ms_access_token()
        if err is not None:
            return err
        return self._graph(access_token).request("GET", "/me/messages?$top=20")


class MicrosoftListEventsTool(_MicrosoftOAuthBase):
    definition = ToolDefinition(
        tool_id="msgraph.list_events",
        name="Microsoft Graph: List Calendar Events",
        description="List upcoming Outlook calendar events.",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks msgraph.list_events"}
        access_token, err = self._ms_access_token()
        if err is not None:
            return err
        return self._graph(access_token).request("GET", "/me/events?$top=20")


class MicrosoftListDriveTool(_MicrosoftOAuthBase):
    definition = ToolDefinition(
        tool_id="msgraph.list_drive",
        name="Microsoft Graph: List OneDrive",
        description="List items in the root of the owner's OneDrive.",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks msgraph.list_drive"}
        access_token, err = self._ms_access_token()
        if err is not None:
            return err
        return self._graph(access_token).request("GET", "/me/drive/root/children")


class MicrosoftSendMailTool(_MicrosoftOAuthBase):
    definition = ToolDefinition(
        tool_id="msgraph.send_mail",
        # Sending email is an external write — PRD Email Send = Tier 2.
        name="Microsoft Graph: Send Mail",
        description="Send an email. payload {to, subject, body}.",
        tier=2,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._network_local_only():
            return {"error": "network_policy=local_only blocks msgraph.send_mail"}
        payload = payload or {}
        to = str(payload.get("to", "")).strip()
        if not to:
            return {"error": "to is required"}
        access_token, err = self._ms_access_token()
        if err is not None:
            return err
        body = {
            "message": {
                "subject": str(payload.get("subject", "")),
                "body": {"contentType": "Text", "content": str(payload.get("body", ""))},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": True,
        }
        return self._graph(access_token).request("POST", "/me/sendMail", body)
