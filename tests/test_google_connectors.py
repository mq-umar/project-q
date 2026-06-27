"""Tests for the Google OAuth2 connector layer (Calendar + Drive tools).

Mirrors tests/test_connectors.py: direct construction with a fake vault + an
injected transport that ROUTES BY URL (token endpoint -> {access_token}, API
hosts -> sample data), plus the full-app harness for registration + tiers.

No live Google credentials are needed: the refresh-token exchange and every API
call go through the injected transport.
"""
from __future__ import annotations

import json
import shutil
import unittest
import urllib.parse
import uuid
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.tools.connectors import (
    GoogleCalendarCreateEventTool,
    GoogleCalendarListEventsTool,
    GoogleDriveListFilesTool,
)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GCAL_EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_GDRIVE_FILES = "https://www.googleapis.com/drive/v3/files"

_FULL_CREDS = {
    "google_client_id": "cid.apps.googleusercontent.com",
    "google_client_secret": "GOCSPX-supersecret",
    "google_refresh_token": "1//refresh-token-secret",
}
_ACCESS_TOKEN = "ya29.test"


class _FakeVault:
    def __init__(self, secrets: dict | None = None) -> None:
        self._secrets = dict(secrets or {})

    def get_secret(self, name: str) -> str:
        if name not in self._secrets:
            raise KeyError(name)
        return self._secrets[name]


class _FakeSettings:
    def __init__(self, network_policy="selected_services") -> None:
        self._network_policy = network_policy

    def get_all(self) -> dict:
        return {"network_policy": self._network_policy}


class _RoutingTransport:
    """Routes by URL: token URL -> token response; API URLs -> sample data.

    `token_response` defaults to a successful access-token mint; set it to a
    dict missing access_token (or {}) to simulate a refresh failure.
    """

    def __init__(self, token_response=None, api_response=None) -> None:
        self.calls: list[dict] = []
        self.token_response = (
            token_response if token_response is not None else {"access_token": _ACCESS_TOKEN}
        )
        self.api_response = api_response if api_response is not None else {"items": []}

    def __call__(self, url, method, headers, body_bytes, timeout):
        self.calls.append(
            {
                "url": url,
                "method": method,
                "headers": headers,
                "body_bytes": body_bytes,
                "timeout": timeout,
            }
        )
        if url == _TOKEN_URL:
            return self.token_response
        return self.api_response


class GoogleConnectorUnitTests(unittest.TestCase):
    # ── Missing ANY of the 3 secrets -> {error}, NO token/transport call ──────
    def test_missing_any_secret_no_transport(self) -> None:
        expected = {
            "error": (
                "google OAuth not configured in the vault (need "
                "google_client_id, google_client_secret, google_refresh_token)"
            )
        }
        # Each subset omits exactly one of the three secrets.
        for omit in _FULL_CREDS:
            partial = {k: v for k, v in _FULL_CREDS.items() if k != omit}
            rec = _RoutingTransport()
            tool = GoogleCalendarListEventsTool(_FakeVault(partial), _FakeSettings(), transport=rec)
            self.assertEqual(tool.execute({}), expected, msg=f"omit={omit}")
            self.assertEqual(rec.calls, [], msg=f"omit={omit}")
        # Empty vault too.
        rec = _RoutingTransport()
        tool = GoogleDriveListFilesTool(_FakeVault(), _FakeSettings(), transport=rec)
        self.assertEqual(tool.execute({}), expected)
        self.assertEqual(rec.calls, [])

    # ── list_events: token refresh (form body) THEN Bearer GET to calendar ───
    def test_list_events_refreshes_then_bearer_get(self) -> None:
        self.assertEqual(GoogleCalendarListEventsTool.definition.tier, 0)
        rec = _RoutingTransport(api_response={"items": [{"id": "e1"}]})
        tool = GoogleCalendarListEventsTool(_FakeVault(_FULL_CREDS), _FakeSettings(), transport=rec)
        result = tool.execute({})
        self.assertEqual(result, {"items": [{"id": "e1"}]})
        self.assertEqual(len(rec.calls), 2)

        token_call = rec.calls[0]
        self.assertEqual(token_call["url"], _TOKEN_URL)
        self.assertEqual(token_call["method"], "POST")
        self.assertEqual(
            token_call["headers"]["Content-Type"], "application/x-www-form-urlencoded"
        )
        # Body is form-urlencoded refresh_token grant carrying the 3 secrets.
        form = urllib.parse.parse_qs(token_call["body_bytes"].decode("utf-8"))
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["client_id"], [_FULL_CREDS["google_client_id"]])
        self.assertEqual(form["client_secret"], [_FULL_CREDS["google_client_secret"]])
        self.assertEqual(form["refresh_token"], [_FULL_CREDS["google_refresh_token"]])

        api_call = rec.calls[1]
        self.assertEqual(api_call["url"], _GCAL_EVENTS)
        self.assertEqual(api_call["method"], "GET")
        self.assertEqual(api_call["headers"]["Authorization"], f"Bearer {_ACCESS_TOKEN}")

    # ── drive list_files: tier 0 Bearer GET ──────────────────────────────────
    def test_drive_list_files_bearer_get(self) -> None:
        self.assertEqual(GoogleDriveListFilesTool.definition.tier, 0)
        rec = _RoutingTransport(api_response={"files": [{"id": "f1"}]})
        tool = GoogleDriveListFilesTool(_FakeVault(_FULL_CREDS), _FakeSettings(), transport=rec)
        result = tool.execute({})
        self.assertEqual(result, {"files": [{"id": "f1"}]})
        api_call = rec.calls[1]
        self.assertEqual(api_call["url"], _GDRIVE_FILES)
        self.assertEqual(api_call["method"], "GET")
        self.assertEqual(api_call["headers"]["Authorization"], f"Bearer {_ACCESS_TOKEN}")

    # ── create_event: tier 1, POSTs {summary, start:{dateTime}, end:{dateTime}}
    def test_create_event_tier1_posts_body(self) -> None:
        self.assertEqual(GoogleCalendarCreateEventTool.definition.tier, 1)
        rec = _RoutingTransport(api_response={"id": "evt1"})
        tool = GoogleCalendarCreateEventTool(_FakeVault(_FULL_CREDS), _FakeSettings(), transport=rec)
        result = tool.execute(
            {"summary": "Sync", "start": "2026-07-01T10:00:00Z", "end": "2026-07-01T11:00:00Z"}
        )
        self.assertEqual(result, {"id": "evt1"})
        api_call = rec.calls[1]
        self.assertEqual(api_call["url"], _GCAL_EVENTS)
        self.assertEqual(api_call["method"], "POST")
        self.assertEqual(api_call["headers"]["Content-Type"], "application/json")
        self.assertEqual(
            json.loads(api_call["body_bytes"].decode("utf-8")),
            {
                "summary": "Sync",
                "start": {"dateTime": "2026-07-01T10:00:00Z"},
                "end": {"dateTime": "2026-07-01T11:00:00Z"},
            },
        )

    def test_create_event_validates_required_fields(self) -> None:
        rec = _RoutingTransport()
        tool = GoogleCalendarCreateEventTool(_FakeVault(_FULL_CREDS), _FakeSettings(), transport=rec)
        self.assertEqual(tool.execute({}), {"error": "summary is required"})
        self.assertEqual(
            tool.execute({"summary": "S"}), {"error": "start and end are required"}
        )
        self.assertEqual(rec.calls, [])

    # ── Secrets NEVER appear in any returned value ────────────────────────────
    def test_secrets_never_appear_in_results(self) -> None:
        secrets = [
            _FULL_CREDS["google_client_secret"],
            _FULL_CREDS["google_refresh_token"],
            _ACCESS_TOKEN,
        ]
        cases = [
            (GoogleCalendarListEventsTool, {}),
            (GoogleDriveListFilesTool, {}),
            (
                GoogleCalendarCreateEventTool,
                {"summary": "S", "start": "2026-07-01T10:00:00Z", "end": "2026-07-01T11:00:00Z"},
            ),
        ]
        for cls, payload in cases:
            for tok_resp in ({"access_token": _ACCESS_TOKEN}, {}):
                rec = _RoutingTransport(token_response=tok_resp)
                tool = cls(_FakeVault(_FULL_CREDS), _FakeSettings(), transport=rec)
                result = tool.execute(payload)
                blob = json.dumps(result)
                for secret in secrets:
                    self.assertNotIn(secret, blob, msg=f"{cls.__name__} leaked secret")

    # ── Token-refresh failure -> generic {error}, no API call ────────────────
    def test_token_refresh_failure(self) -> None:
        rec = _RoutingTransport(token_response={"error": "invalid_grant"})
        tool = GoogleCalendarListEventsTool(_FakeVault(_FULL_CREDS), _FakeSettings(), transport=rec)
        result = tool.execute({})
        self.assertEqual(result, {"error": "google token refresh failed"})
        # Only the token call happened; no API call with a bad/absent token.
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0]["url"], _TOKEN_URL)

    def test_token_response_missing_access_token(self) -> None:
        rec = _RoutingTransport(token_response={"token_type": "Bearer"})
        tool = GoogleDriveListFilesTool(_FakeVault(_FULL_CREDS), _FakeSettings(), transport=rec)
        self.assertEqual(tool.execute({}), {"error": "google token refresh failed"})
        self.assertEqual(len(rec.calls), 1)

    # ── network_policy=local_only blocks BEFORE any call (no token mint) ──────
    def test_local_only_blocks_before_any_call(self) -> None:
        for cls, tool_id in (
            (GoogleCalendarListEventsTool, "google_calendar.list_events"),
            (GoogleCalendarCreateEventTool, "google_calendar.create_event"),
            (GoogleDriveListFilesTool, "google_drive.list_files"),
        ):
            rec = _RoutingTransport()
            tool = cls(
                _FakeVault(_FULL_CREDS),
                _FakeSettings(network_policy="local_only"),
                transport=rec,
            )
            self.assertEqual(
                tool.execute({}),
                {"error": f"network_policy=local_only blocks {tool_id}"},
            )
            self.assertEqual(rec.calls, [])

    # ── execute never raises (transport blowups collapse to {error}) ─────────
    def test_execute_never_raises_on_transport_blowup(self) -> None:
        class _Boom:
            def __call__(self, *a, **k):
                raise RuntimeError("boom-with-secret")

        tool = GoogleDriveListFilesTool(_FakeVault(_FULL_CREDS), _FakeSettings(), transport=_Boom())
        self.assertEqual(tool.execute({}), {"error": "google token refresh failed"})


class GoogleConnectorAppHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            project_name="Project Q Google Connector Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            port=8903,
        )
        self.app = create_application(config)

    def tearDown(self) -> None:
        shutdown = getattr(self.app, "shutdown_services", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def test_google_connectors_registered_with_tiers(self) -> None:
        expected = {
            "google_calendar.list_events": 0,
            "google_calendar.create_event": 1,
            "google_drive.list_files": 0,
        }
        for tool_id, tier in expected.items():
            tool = self.app.tools.get(tool_id)
            self.assertIsNotNone(tool, msg=f"{tool_id} not registered")
            self.assertEqual(tool.definition.tier, tier)

    def test_default_path_no_creds_returns_error(self) -> None:
        result = self.app.tools.get("google_calendar.list_events").execute({})
        self.assertEqual(
            result,
            {
                "error": (
                    "google OAuth not configured in the vault (need "
                    "google_client_id, google_client_secret, google_refresh_token)"
                )
            },
        )


if __name__ == "__main__":
    unittest.main()
