from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from project_q_relay.service import RelayService
from project_q_relay.storage import RelayStore


class RelayServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="project-q-relay-"))
        self.db_path = self.root / "relay.db"
        self.store = RelayStore(self.db_path)
        self.service = RelayService(self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_device_tokens_are_hashed_restart_safe_and_revocable(self) -> None:
        registration = self.service.register_device(
            owner_id="owner_1",
            device_id="windows_1",
            role="windows",
            ttl_seconds=3600,
        )

        self.assertEqual(
            self.service.authenticate(registration["token"])["device_id"],
            "windows_1",
        )
        database_bytes = self.db_path.read_bytes()
        self.assertNotIn(registration["token"].encode("utf-8"), database_bytes)
        self.assertIn(
            hashlib.sha256(registration["token"].encode("utf-8")).hexdigest().encode("ascii"),
            database_bytes,
        )

        restarted = RelayService(RelayStore(self.db_path))
        self.assertEqual(
            restarted.authenticate(registration["token"])["role"],
            "windows",
        )
        restarted.revoke_device(
            actor_device_id="windows_1",
            target_device_id="windows_1",
            reason="owner revoked relay access",
        )
        with self.assertRaises(PermissionError):
            restarted.authenticate(registration["token"])

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT revoked_at FROM relay_devices WHERE device_id = 'windows_1'"
            ).fetchone()
        self.assertIsNotNone(row[0])

    def test_opaque_envelopes_are_recipient_scoped_bounded_and_idempotent(self) -> None:
        windows = self.service.register_device(
            owner_id="owner_1",
            device_id="windows_1",
            role="windows",
        )
        phone = self.service.register_device(
            owner_id="owner_1",
            device_id="phone_1",
            role="phone",
        )
        outsider = self.service.register_device(
            owner_id="owner_2",
            device_id="phone_2",
            role="phone",
        )
        envelope = {
            "protocol_version": 2,
            "algorithm": "X25519-HKDF-SHA256-AES-256-GCM",
            "message_id": "message_1",
            "sender_device_id": "windows_1",
            "recipient_device_id": "phone_1",
            "event_kind": "sync_event",
            "sequence_number": 1,
            "created_at": "2026-06-07T12:00:00Z",
            "nonce": "opaque-nonce",
            "ciphertext": "opaque-ciphertext",
            "tag": "opaque-tag",
            "content_type": "application/json",
        }

        first = self.service.upload_envelope(
            token=windows["token"],
            envelope=envelope,
        )
        duplicate = self.service.upload_envelope(
            token=windows["token"],
            envelope=envelope,
        )
        inbox = self.service.pull_envelopes(
            token=phone["token"],
            limit=20,
        )

        self.assertEqual(first["status"], "stored")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual([item["message_id"] for item in inbox["items"]], ["message_1"])
        self.assertEqual(inbox["items"][0]["ciphertext"], "opaque-ciphertext")
        self.assertEqual(
            self.service.pull_envelopes(token=outsider["token"], limit=20)["items"],
            [],
        )
        first_ack = self.service.acknowledge_envelope(
            token=phone["token"],
            message_id="message_1",
        )
        second_ack = self.service.acknowledge_envelope(
            token=phone["token"],
            message_id="message_1",
        )
        self.assertEqual(first_ack["acknowledged_at"], second_ack["acknowledged_at"])
        self.assertEqual(
            self.service.pull_envelopes(token=phone["token"], limit=20)["items"],
            [],
        )

        with self.assertRaises(ValueError):
            self.service.upload_envelope(
                token=windows["token"],
                envelope={**envelope, "message_id": "message_plaintext", "payload": "secret"},
            )
        with self.assertRaises(ValueError):
            self.service.upload_envelope(
                token=windows["token"],
                envelope={
                    **envelope,
                    "message_id": "message_oversized",
                    "ciphertext": "x" * (600 * 1024),
                },
            )

        with sqlite3.connect(self.db_path) as conn:
            stored = conn.execute(
                "SELECT envelope_json FROM relay_envelopes WHERE message_id = 'message_1'"
            ).fetchone()[0]
        self.assertEqual(json.loads(stored), envelope)

    def test_presence_is_owner_scoped_and_expires(self) -> None:
        windows = self.service.register_device(
            owner_id="owner_1",
            device_id="windows_1",
            role="windows",
        )
        phone = self.service.register_device(
            owner_id="owner_1",
            device_id="phone_1",
            role="phone",
        )
        outsider = self.service.register_device(
            owner_id="owner_2",
            device_id="phone_2",
            role="phone",
        )

        presence = self.service.publish_presence(
            token=phone["token"],
            state="active",
            ttl_seconds=999,
        )
        visible = self.service.list_presence(token=windows["token"])
        hidden = self.service.list_presence(token=outsider["token"])

        self.assertEqual(presence["state"], "active")
        self.assertEqual([item["device_id"] for item in visible["items"]], ["phone_1"])
        self.assertEqual(hidden["items"], [])
        with self.store.connection() as conn:
            conn.execute(
                "UPDATE relay_presence SET expires_at = '2000-01-01T00:00:00Z'"
            )
        self.assertEqual(
            self.service.list_presence(token=windows["token"])["items"],
            [],
        )

    def test_fastapi_routes_deliver_opaque_messages_and_enforce_bearer_scope(self) -> None:
        from fastapi.testclient import TestClient
        from project_q_relay.app import create_relay_app

        windows = self.service.register_device(
            owner_id="owner_1",
            device_id="windows_1",
            role="windows",
        )
        phone = self.service.register_device(
            owner_id="owner_1",
            device_id="phone_1",
            role="phone",
        )
        envelope = {
            "protocol_version": 2,
            "algorithm": "X25519-HKDF-SHA256-AES-256-GCM",
            "message_id": "message_http_1",
            "sender_device_id": "windows_1",
            "recipient_device_id": "phone_1",
            "event_kind": "approval",
            "sequence_number": 1,
            "created_at": "2026-06-07T12:00:00Z",
            "nonce": "opaque-nonce",
            "ciphertext": "opaque-ciphertext",
            "tag": "opaque-tag",
            "content_type": "application/json",
        }
        client = TestClient(create_relay_app(service=self.service))

        self.assertEqual(client.get("/health").status_code, 200)
        uploaded = client.post(
            "/v1/envelopes",
            headers={"Authorization": f"Bearer {windows['token']}"},
            json=envelope,
        )
        self.assertEqual(uploaded.status_code, 201)
        pulled = client.get(
            "/v1/envelopes",
            headers={"Authorization": f"Bearer {phone['token']}"},
        )
        self.assertEqual(pulled.json()["items"][0]["message_id"], "message_http_1")
        self.assertEqual(client.get("/v1/envelopes").status_code, 401)
        acknowledged = client.post(
            "/v1/envelopes/message_http_1/ack",
            headers={"Authorization": f"Bearer {phone['token']}"},
        )
        self.assertEqual(acknowledged.status_code, 200)

    def test_bootstrap_provisioning_is_owner_scoped_and_not_stored(self) -> None:
        from fastapi.testclient import TestClient
        from project_q_relay.app import create_relay_app
        from project_q_relay.config import RelayConfig

        bootstrap_token = "bootstrap-" + ("x" * 40)
        config = RelayConfig(
            database_path=self.db_path,
            bootstrap_token=bootstrap_token,
            owner_id="owner_provisioned",
        )
        client = TestClient(
            create_relay_app(service=self.service, config=config)
        )

        unauthorized = client.post(
            "/v1/admin/devices",
            json={"device_id": "phone_provisioned", "role": "phone"},
        )
        self.assertEqual(unauthorized.status_code, 401)

        provisioned = client.post(
            "/v1/admin/devices",
            headers={"X-Project-Q-Relay-Bootstrap": bootstrap_token},
            json={"device_id": "phone_provisioned", "role": "phone"},
        )
        self.assertEqual(provisioned.status_code, 201)
        payload = provisioned.json()
        self.assertEqual(payload["owner_id"], "owner_provisioned")
        self.assertEqual(payload["device_id"], "phone_provisioned")
        self.assertEqual(payload["role"], "phone")
        self.assertNotIn(bootstrap_token.encode("utf-8"), self.db_path.read_bytes())
        self.assertEqual(
            self.service.authenticate(payload["token"])["device_id"],
            "phone_provisioned",
        )

    def test_websocket_delivers_unacknowledged_envelopes_and_reconnects(self) -> None:
        from fastapi.testclient import TestClient
        from project_q_relay.app import create_relay_app

        windows = self.service.register_device(
            owner_id="owner_1",
            device_id="windows_1",
            role="windows",
        )
        phone = self.service.register_device(
            owner_id="owner_1",
            device_id="phone_1",
            role="phone",
        )
        envelope = {
            "protocol_version": 2,
            "algorithm": "X25519-HKDF-SHA256-AES-256-GCM",
            "message_id": "message_ws_1",
            "sender_device_id": "windows_1",
            "recipient_device_id": "phone_1",
            "event_kind": "sync_event",
            "sequence_number": 1,
            "created_at": "2026-06-07T12:00:00Z",
            "nonce": "opaque-nonce",
            "ciphertext": "opaque-ciphertext",
            "tag": "opaque-tag",
            "content_type": "application/json",
        }
        client = TestClient(create_relay_app(service=self.service))

        with client.websocket_connect(f"/v1/ws?token={phone['token']}") as socket:
            uploaded = client.post(
                "/v1/envelopes",
                headers={"Authorization": f"Bearer {windows['token']}"},
                json=envelope,
            )
            self.assertEqual(uploaded.status_code, 201)
            delivered = socket.receive_json()
            self.assertEqual(delivered["type"], "envelope")
            self.assertEqual(delivered["envelope"]["message_id"], "message_ws_1")

        with client.websocket_connect(f"/v1/ws?token={phone['token']}") as socket:
            redelivered = socket.receive_json()
            self.assertEqual(redelivered["envelope"]["message_id"], "message_ws_1")

    def test_apns_wake_adapter_receives_only_opaque_routing_metadata(self) -> None:
        from project_q_relay.apns import RecordingAPNsAdapter

        adapter = RecordingAPNsAdapter()
        service = RelayService(self.store, apns_adapter=adapter)
        windows = service.register_device(
            owner_id="owner_1",
            device_id="windows_1",
            role="windows",
        )
        service.register_device(
            owner_id="owner_1",
            device_id="phone_1",
            role="phone",
        )
        service.upload_envelope(
            token=windows["token"],
            envelope={
                "protocol_version": 2,
                "algorithm": "X25519-HKDF-SHA256-AES-256-GCM",
                "message_id": "message_apns_1",
                "sender_device_id": "windows_1",
                "recipient_device_id": "phone_1",
                "event_kind": "approval",
                "sequence_number": 1,
                "created_at": "2026-06-07T12:00:00Z",
                "nonce": "opaque-nonce",
                "ciphertext": "private-ciphertext",
                "tag": "opaque-tag",
                "content_type": "application/json",
            },
        )

        self.assertEqual(
            adapter.requests,
            [
                {
                    "recipient_device_id": "phone_1",
                    "message_kind": "approval",
                    "routing_id": "message_apns_1",
                    "collapse_id": "project-q-phone_1-approval",
                }
            ],
        )
        self.assertNotIn("private-ciphertext", json.dumps(adapter.requests))

    def test_apns_device_routes_are_encrypted_and_phone_scoped(self) -> None:
        route_key = bytes(range(32))
        service = RelayService(self.store, apns_route_key=route_key)
        windows = service.register_device(
            owner_id="owner_1",
            device_id="windows_1",
            role="windows",
        )
        phone = service.register_device(
            owner_id="owner_1",
            device_id="phone_1",
            role="phone",
        )
        device_token = "ab" * 32

        registered = service.register_apns_route(
            token=phone["token"],
            device_token=device_token,
            environment="development",
        )
        self.assertEqual(registered["device_id"], "phone_1")
        self.assertEqual(registered["environment"], "development")
        database_bytes = self.db_path.read_bytes()
        self.assertNotIn(device_token.encode("ascii"), database_bytes)
        route = service.apns_routes.get("phone_1")
        self.assertEqual(route["device_token"], device_token)

        with self.assertRaises(PermissionError):
            service.register_apns_route(
                token=windows["token"],
                device_token=device_token,
                environment="development",
            )

    def test_apns_registration_route_requires_bearer_and_configured_key(self) -> None:
        from fastapi.testclient import TestClient
        from project_q_relay.app import create_relay_app
        from project_q_relay.config import RelayConfig

        phone = self.service.register_device(
            owner_id="owner_1",
            device_id="phone_1",
            role="phone",
        )
        configured = RelayService(
            self.store,
            apns_route_key=bytes(range(32)),
        )
        client = TestClient(create_relay_app(service=configured))
        response = client.post(
            "/v1/apns/register",
            headers={"Authorization": f"Bearer {phone['token']}"},
            json={
                "device_token": "cd" * 32,
                "environment": "development",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["device_id"], "phone_1")

        unconfigured = TestClient(
            create_relay_app(
                service=self.service,
                config=RelayConfig(database_path=self.db_path),
            )
        )
        disabled = unconfigured.post(
            "/v1/apns/register",
            headers={"Authorization": f"Bearer {phone['token']}"},
            json={
                "device_token": "ef" * 32,
                "environment": "development",
            },
        )
        self.assertEqual(disabled.status_code, 503)

    def test_production_apns_adapter_sends_only_generic_routing_notification(self) -> None:
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from project_q_relay.apns import APNsRouteStore, ProductionAPNsAdapter

        route_store = APNsRouteStore(self.store, bytes(range(32)))
        route_store.set(
            device_id="phone_1",
            device_token="12" * 32,
            environment="development",
            updated_at="2026-06-07T12:00:00Z",
        )
        private_key = ec.generate_private_key(ec.SECP256R1())
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        class FakeResponse:
            status_code = 200
            text = ""

        class FakeHTTPClient:
            def __init__(self) -> None:
                self.requests = []

            def post(self, url, *, headers, json):
                self.requests.append(
                    {"url": url, "headers": headers, "json": json}
                )
                return FakeResponse()

        client = FakeHTTPClient()
        adapter = ProductionAPNsAdapter(
            route_store=route_store,
            team_id="TEAM123",
            key_id="KEY123",
            bundle_id="com.projectq.companion",
            private_key_pem=private_pem,
            http_client=client,
        )
        adapter.send_wake(
            recipient_device_id="phone_1",
            message_kind="approval",
            routing_id="opaque-message-1",
            collapse_id="project-q-phone_1-approval",
        )

        request = client.requests[0]
        self.assertTrue(
            request["url"].startswith(
                "https://api.sandbox.push.apple.com/3/device/"
            )
        )
        self.assertEqual(request["headers"]["apns-topic"], "com.projectq.companion")
        self.assertEqual(request["headers"]["apns-push-type"], "alert")
        self.assertTrue(request["headers"]["authorization"].startswith("bearer "))
        self.assertEqual(
            request["json"]["project_q"]["routing_id"],
            "opaque-message-1",
        )
        self.assertEqual(request["json"]["route"], "approvals")
        encoded = json.dumps(request["json"])
        self.assertNotIn("ciphertext", encoded)
        self.assertNotIn("private", encoded)
        jwt_parts = request["headers"]["authorization"][7:].split(".")
        self.assertEqual(len(jwt_parts), 3)
        signature = base64.urlsafe_b64decode(
            jwt_parts[2] + ("=" * (-len(jwt_parts[2]) % 4))
        )
        self.assertEqual(len(signature), 64)


if __name__ == "__main__":
    unittest.main()
