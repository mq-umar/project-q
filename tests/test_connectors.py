"""Tests for the bearer-token connector framework (GitHub + Slack tools).

Two layers: direct construction with a fake vault + injected transport (recording
URL/method/headers/body without network), and the full-app harness (mirrors
tests/test_audit_fixes.py) for registration + tier + real-vault wiring.
"""
from __future__ import annotations

import json
import shutil
import unittest
import uuid
from pathlib import Path

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.tools.connectors import (
    GitHubCreateIssueTool,
    GitHubListIssuesTool,
    GitHubListReposTool,
    SlackListChannelsTool,
    SlackPostMessageTool,
)


class _FakeVault:
    def __init__(self, secrets: dict | None = None) -> None:
        self._secrets = dict(secrets or {})

    def get_secret(self, name: str) -> str:
        if name not in self._secrets:
            raise KeyError(name)
        return self._secrets[name]


class _RecordingTransport:
    def __init__(self, response=None, raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.response = response if response is not None else {"ok": True}
        self.raises = raises

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
        if self.raises is not None:
            raise self.raises
        return self.response


class _FakeSettings:
    def __init__(self, network_policy="selected_services") -> None:
        self._network_policy = network_policy

    def get_all(self) -> dict:
        return {"network_policy": self._network_policy}


class ConnectorUnitTests(unittest.TestCase):
    # ── Missing token -> {error}, no transport call ──────────────────────────
    def test_github_missing_token_no_transport(self) -> None:
        rec = _RecordingTransport()
        tool = GitHubListReposTool(_FakeVault(), _FakeSettings(), transport=rec)
        result = tool.execute({})
        self.assertEqual(result, {"error": "github_token not configured in the vault"})
        self.assertEqual(rec.calls, [])

    def test_slack_missing_token_no_transport(self) -> None:
        rec = _RecordingTransport()
        tool = SlackListChannelsTool(_FakeVault(), _FakeSettings(), transport=rec)
        result = tool.execute({})
        self.assertEqual(result, {"error": "slack_bot_token not configured in the vault"})
        self.assertEqual(rec.calls, [])

    # ── list_repos happy path ────────────────────────────────────────────────
    def test_list_repos_builds_request(self) -> None:
        rec = _RecordingTransport(response={"repos": [1, 2]})
        tool = GitHubListReposTool(_FakeVault({"github_token": "ghp_x"}), _FakeSettings(), transport=rec)
        result = tool.execute({})
        self.assertEqual(result, {"repos": [1, 2]})
        call = rec.calls[0]
        self.assertEqual(call["url"], "https://api.github.com/user/repos")
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["headers"]["Authorization"], "Bearer ghp_x")
        self.assertEqual(call["headers"]["Accept"], "application/vnd.github+json")

    def test_list_issues_builds_repo_path(self) -> None:
        rec = _RecordingTransport(response=[])
        tool = GitHubListIssuesTool(_FakeVault({"github_token": "ghp_x"}), _FakeSettings(), transport=rec)
        tool.execute({"repo": "octo/hello"})
        call = rec.calls[0]
        self.assertEqual(call["url"], "https://api.github.com/repos/octo/hello/issues")
        self.assertEqual(call["method"], "GET")

    def test_create_issue_tier2_posts_body(self) -> None:
        self.assertEqual(GitHubCreateIssueTool.definition.tier, 2)
        rec = _RecordingTransport(response={"number": 5})
        tool = GitHubCreateIssueTool(_FakeVault({"github_token": "ghp_x"}), _FakeSettings(), transport=rec)
        tool.execute({"repo": "octo/hello", "title": "T", "body": "B"})
        call = rec.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://api.github.com/repos/octo/hello/issues")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(call["body_bytes"].decode("utf-8")), {"title": "T", "body": "B"})

    def test_slack_post_message_tier2(self) -> None:
        self.assertEqual(SlackPostMessageTool.definition.tier, 2)
        rec = _RecordingTransport(response={"ok": True})
        tool = SlackPostMessageTool(_FakeVault({"slack_bot_token": "xoxb-1"}), _FakeSettings(), transport=rec)
        tool.execute({"channel": "C1", "text": "hi"})
        call = rec.calls[0]
        self.assertEqual(call["url"], "https://slack.com/api/chat.postMessage")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["Authorization"], "Bearer xoxb-1")
        self.assertEqual(json.loads(call["body_bytes"].decode("utf-8")), {"channel": "C1", "text": "hi"})

    def test_slack_list_channels_tier0_get(self) -> None:
        self.assertEqual(SlackListChannelsTool.definition.tier, 0)
        rec = _RecordingTransport(response={"ok": True, "channels": []})
        tool = SlackListChannelsTool(_FakeVault({"slack_bot_token": "xoxb-1"}), _FakeSettings(), transport=rec)
        tool.execute({})
        call = rec.calls[0]
        self.assertEqual(call["url"], "https://slack.com/api/conversations.list")
        self.assertEqual(call["method"], "GET")

    # ── Slack ok:false is passed through verbatim (no error synthesis) ───────
    def test_slack_ok_false_passthrough(self) -> None:
        rec = _RecordingTransport(response={"ok": False, "error": "channel_not_found"})
        tool = SlackPostMessageTool(_FakeVault({"slack_bot_token": "x"}), _FakeSettings(), transport=rec)
        result = tool.execute({"channel": "C1", "text": "hi"})
        self.assertEqual(result, {"ok": False, "error": "channel_not_found"})

    # ── local_only blocks before token resolution / transport ───────────────
    def test_local_only_blocks_without_transport(self) -> None:
        rec = _RecordingTransport()
        tool = GitHubListReposTool(
            _FakeVault({"github_token": "ghp_x"}),
            _FakeSettings(network_policy="local_only"),
            transport=rec,
        )
        result = tool.execute({})
        self.assertEqual(result, {"error": "network_policy=local_only blocks github.list_repos"})
        self.assertEqual(rec.calls, [])

    # ── execute never raises ─────────────────────────────────────────────────
    def test_execute_never_raises(self) -> None:
        rec = _RecordingTransport(raises=RuntimeError("boom"))
        tool = GitHubListReposTool(_FakeVault({"github_token": "ghp_x"}), _FakeSettings(), transport=rec)
        result = tool.execute({})
        self.assertEqual(result, {"error": "boom"})

    # ── invalid repo is rejected before any token/transport use ──────────────
    def test_invalid_repo_is_rejected(self) -> None:
        rec = _RecordingTransport()
        tool = GitHubListIssuesTool(
            _FakeVault({"github_token": "ghp_x"}), _FakeSettings(), transport=rec
        )
        for bad in ("octo", "octo/hello/extra", "octo/hello?x=1", "octo /hello"):
            self.assertEqual(
                tool.execute({"repo": bad}),
                {"error": "invalid repo (expected owner/name)"},
            )
        self.assertEqual(rec.calls, [])


class ConnectorAppHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).resolve().parent / ".tmp" / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            project_name="Project Q Connector Test",
            workspace_root=self.root,
            data_root=self.root / ".project_q",
            db_path=self.root / ".project_q" / "project_q.db",
            port=8902,
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

    def test_connectors_registered_with_tiers(self) -> None:
        expected = {
            "github.list_repos": 0,
            "github.list_issues": 0,
            "github.create_issue": 2,
            "slack.list_channels": 0,
            "slack.post_message": 2,
        }
        for tool_id, tier in expected.items():
            tool = self.app.tools.get(tool_id)
            self.assertIsNotNone(tool)
            self.assertEqual(tool.definition.tier, tier)

    def test_default_path_no_secret_returns_error(self) -> None:
        result = self.app.tools.get("github.list_repos").execute({})
        self.assertEqual(result, {"error": "github_token not configured in the vault"})


if __name__ == "__main__":
    unittest.main()
