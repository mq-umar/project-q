"""Tests for the Microsoft Graph OAuth2 connectors (no live credentials needed)."""
from __future__ import annotations

import json
import shutil
import unittest
import uuid
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.tools.connectors import (
    _MS_TOKEN_URL,
    MicrosoftListDriveTool,
    MicrosoftListEventsTool,
    MicrosoftListMailTool,
    MicrosoftSendMailTool,
)

_FULL = {"ms_client_id": "cid", "ms_client_secret": "csecret", "ms_refresh_token": "rtoken"}


class _FakeVault:
    def __init__(self, secrets=None) -> None:
        self._s = dict(secrets or {})

    def get_secret(self, name: str) -> str:
        if name not in self._s:
            raise KeyError(name)
        return self._s[name]


class _FakeSettings:
    def __init__(self, network_policy: str = "selected_services") -> None:
        self._np = network_policy

    def get_all(self) -> dict:
        return {"network_policy": self._np}


class _RoutingTransport:
    """Routes by URL: the token URL returns a token; everything else returns API data."""

    def __init__(self, token_response=None, api_response=None, raises=None) -> None:
        self.calls: list[dict] = []
        self.token_response = token_response if token_response is not None else {"access_token": "ms_access_xyz"}
        self.api_response = api_response if api_response is not None else {"value": []}
        self.raises = raises

    def __call__(self, url, method, headers, body_bytes, timeout):
        self.calls.append({"url": url, "method": method, "headers": headers, "body_bytes": body_bytes})
        if self.raises is not None:
            raise self.raises
        return self.token_response if url == _MS_TOKEN_URL else self.api_response


class MicrosoftConnectorTests(unittest.TestCase):
    def test_missing_secret_no_call(self) -> None:
        rec = _RoutingTransport()
        tool = MicrosoftListMailTool(_FakeVault({"ms_client_id": "cid"}), _FakeSettings(), transport=rec)
        self.assertIn("microsoft OAuth not configured", tool.execute({})["error"])
        self.assertEqual(rec.calls, [])

    def test_list_mail_refreshes_then_bearer_get(self) -> None:
        rec = _RoutingTransport(api_response={"value": [{"subject": "hi"}]})
        tool = MicrosoftListMailTool(_FakeVault(_FULL), _FakeSettings(), transport=rec)
        self.assertEqual(tool.execute({}), {"value": [{"subject": "hi"}]})
        self.assertEqual(rec.calls[0]["url"], _MS_TOKEN_URL)
        self.assertIn(b"grant_type=refresh_token", rec.calls[0]["body_bytes"])
        self.assertEqual(rec.calls[1]["url"], "https://graph.microsoft.com/v1.0/me/messages?$top=20")
        self.assertEqual(rec.calls[1]["headers"]["Authorization"], "Bearer ms_access_xyz")

    def test_list_events_and_drive_paths(self) -> None:
        for tool_cls, suffix in (
            (MicrosoftListEventsTool, "/me/events?$top=20"),
            (MicrosoftListDriveTool, "/me/drive/root/children"),
        ):
            rec = _RoutingTransport()
            tool_cls(_FakeVault(_FULL), _FakeSettings(), transport=rec).execute({})
            self.assertEqual(rec.calls[1]["url"], "https://graph.microsoft.com/v1.0" + suffix)
            self.assertEqual(rec.calls[1]["method"], "GET")

    def test_send_mail_tier2_builds_body_and_requires_to(self) -> None:
        self.assertEqual(MicrosoftSendMailTool.definition.tier, 2)
        rec = _RoutingTransport()
        tool = MicrosoftSendMailTool(_FakeVault(_FULL), _FakeSettings(), transport=rec)
        self.assertEqual(tool.execute({"subject": "s"}), {"error": "to is required"})
        self.assertEqual(rec.calls, [])
        rec2 = _RoutingTransport(api_response={"status": "ok"})
        MicrosoftSendMailTool(_FakeVault(_FULL), _FakeSettings(), transport=rec2).execute(
            {"to": "a@b.com", "subject": "Hi", "body": "Yo"}
        )
        api = rec2.calls[1]
        self.assertEqual(api["url"], "https://graph.microsoft.com/v1.0/me/sendMail")
        body = json.loads(api["body_bytes"].decode("utf-8"))
        self.assertEqual(body["message"]["toRecipients"][0]["emailAddress"]["address"], "a@b.com")

    def test_secrets_never_leak(self) -> None:
        rec = _RoutingTransport()
        tool = MicrosoftListMailTool(_FakeVault(_FULL), _FakeSettings(), transport=rec)
        blob = json.dumps(tool.execute({}))
        for secret in ("csecret", "rtoken", "ms_access_xyz"):
            self.assertNotIn(secret, blob)

    def test_token_failure_is_generic(self) -> None:
        rec = _RoutingTransport(token_response={"error": "bad"})
        tool = MicrosoftListMailTool(_FakeVault(_FULL), _FakeSettings(), transport=rec)
        self.assertEqual(tool.execute({}), {"error": "microsoft token refresh failed"})

    def test_local_only_blocks(self) -> None:
        rec = _RoutingTransport()
        tool = MicrosoftListMailTool(_FakeVault(_FULL), _FakeSettings(network_policy="local_only"), transport=rec)
        self.assertEqual(tool.execute({}), {"error": "network_policy=local_only blocks msgraph.list_mail"})
        self.assertEqual(rec.calls, [])

    def test_never_raises_on_transport_blowup(self) -> None:
        rec = _RoutingTransport(raises=RuntimeError("boom-secret"))
        tool = MicrosoftListMailTool(_FakeVault(_FULL), _FakeSettings(), transport=rec)
        self.assertEqual(tool.execute({}), {"error": "microsoft token refresh failed"})


class MicrosoftAppHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            project_name="MS Connector Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            port=8904,
        )
        self.app = create_application(config)

    def tearDown(self) -> None:
        sd = getattr(self.app, "shutdown_services", None)
        if callable(sd):
            try:
                sd()
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def test_registered_with_tiers(self) -> None:
        expected = {
            "msgraph.list_mail": 0,
            "msgraph.list_events": 0,
            "msgraph.list_drive": 0,
            "msgraph.send_mail": 2,
        }
        for tool_id, tier in expected.items():
            tool = self.app.tools.get(tool_id)
            self.assertIsNotNone(tool)
            self.assertEqual(tool.definition.tier, tier)


if __name__ == "__main__":
    unittest.main()
