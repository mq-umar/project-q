from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for source_root in (ROOT / "src", ROOT / "relay"):
    source_text = str(source_root)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)

from fastapi.testclient import TestClient

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from project_q.app import create_application
from project_q.config import AppConfig
from project_q.services.companion_crypto import CompanionCryptoService
from project_q.services.relay import (
    CompanionRelayBridgeService,
    CompanionRelayCommandDispatcher,
)
from project_q_relay.apns import RecordingAPNsAdapter
from project_q_relay.app import create_relay_app
from project_q_relay.service import RelayService
from project_q_relay.storage import RelayStore


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="project-q-relay-verify-") as temp_dir:
        temp_root = Path(temp_dir)
        adapter = RecordingAPNsAdapter()
        service = RelayService(
            RelayStore(temp_root / "relay.db"),
            apns_adapter=adapter,
        )
        windows = service.register_device(
            owner_id="verification-owner",
            device_id="verification-windows",
            role="windows",
        )
        phone = service.register_device(
            owner_id="verification-owner",
            device_id="verification-phone",
            role="phone",
        )
        client = TestClient(create_relay_app(service=service))
        envelope = {
            "protocol_version": 2,
            "algorithm": "X25519-HKDF-SHA256-AES-256-GCM",
            "message_id": "verification-message",
            "sender_device_id": "verification-windows",
            "recipient_device_id": "verification-phone",
            "event_kind": "approval",
            "sequence_number": 1,
            "created_at": "2026-06-07T00:00:00Z",
            "nonce": "opaque-nonce",
            "ciphertext": "opaque-ciphertext",
            "tag": "opaque-tag",
            "content_type": "application/json",
        }

        assert client.get("/health").status_code == 200
        with client.websocket_connect(f"/v1/ws?token={phone['token']}") as socket:
            response = client.post(
                "/v1/envelopes",
                headers={"Authorization": f"Bearer {windows['token']}"},
                json=envelope,
            )
            assert response.status_code == 201, response.text
            delivered = socket.receive_json()
            assert delivered["envelope"] == envelope

        pulled = client.get(
            "/v1/envelopes",
            headers={"Authorization": f"Bearer {phone['token']}"},
        )
        assert pulled.json()["items"] == [envelope]
        acknowledged = client.post(
            "/v1/envelopes/verification-message/ack",
            headers={"Authorization": f"Bearer {phone['token']}"},
        )
        assert acknowledged.status_code == 200
        assert client.get(
            "/v1/envelopes",
            headers={"Authorization": f"Bearer {phone['token']}"},
        ).json()["items"] == []

        presence = client.post(
            "/v1/presence",
            headers={"Authorization": f"Bearer {phone['token']}"},
            json={"state": "active", "ttl_seconds": 120},
        )
        assert presence.status_code == 200
        assert client.get(
            "/v1/presence",
            headers={"Authorization": f"Bearer {windows['token']}"},
        ).json()["items"][0]["device_id"] == "verification-phone"

        revoked = client.post(
            "/v1/devices/verification-phone/revoke",
            headers={"Authorization": f"Bearer {windows['token']}"},
            json={"reason": "verification complete"},
        )
        assert revoked.status_code == 200
        assert client.get(
            "/v1/envelopes",
            headers={"Authorization": f"Bearer {phone['token']}"},
        ).status_code == 401
        assert adapter.requests[0]["routing_id"] == "verification-message"

        app = create_application(
            AppConfig(
                project_name="Project Q Relay Verification",
                workspace_root=temp_root,
                data_root=temp_root / ".project_q",
                db_path=temp_root / ".project_q" / "project_q.db",
            )
        )
        crypto = CompanionCryptoService()
        phone_key_pair = crypto.generate_key_pair()
        approval_private_key = ec.generate_private_key(ec.SECP256R1())
        approval_public_key = base64.urlsafe_b64encode(
            approval_private_key.public_key().public_bytes(
                encoding=serialization.Encoding.X962,
                format=serialization.PublicFormat.UncompressedPoint,
            )
        ).decode("ascii").rstrip("=")
        pairing = app.companion.start_pairing_v2(
            device_name="Relay Verification iPhone",
            platform="ios",
        )
        app.companion.complete_pairing_v2(
            device_id=pairing["device_id"],
            pairing_token=pairing["pairing_token"],
            key_agreement_public_key=phone_key_pair.public_key,
            approval_public_key=approval_public_key,
        )
        token_bytes = base64.urlsafe_b64decode(
            pairing["pairing_token"]
            + ("=" * (-len(pairing["pairing_token"]) % 4))
        )
        phone_keys = crypto.derive_session_keys(
            private_key=phone_key_pair.private_key,
            peer_public_key=pairing["windows_key_agreement_public_key"],
            pairing_token=token_bytes,
            local_device_id=pairing["device_id"],
            remote_device_id=pairing["windows_device_id"],
        )
        relay_windows = service.register_device(
            owner_id="bridge-owner",
            device_id=pairing["windows_device_id"],
            role="windows",
        )
        relay_phone = service.register_device(
            owner_id="bridge-owner",
            device_id=pairing["device_id"],
            role="phone",
        )

        class InProcessRelayClient:
            def upload(self, token, envelope):
                return service.upload_envelope(token=token, envelope=envelope)

            def pull(self, token, limit=200):
                return service.pull_envelopes(token=token, limit=limit)

            def acknowledge(self, token, message_id):
                return service.acknowledge_envelope(
                    token=token,
                    message_id=message_id,
                )

        relay_client = InProcessRelayClient()
        bridge = CompanionRelayBridgeService(
            db=app.db,
            protocol_service=app.companion_protocol,
            relay_client=relay_client,
            windows_token=relay_windows["token"],
            windows_device_id=pairing["windows_device_id"],
            command_handler=CompanionRelayCommandDispatcher(app).handle,
        )
        command_payload = {
            "command_id": "verification-command-1",
            "method": "POST",
            "path": "/api/companion/v2/quick-capture",
            "body": base64.b64encode(
                json.dumps(
                    {
                        "text": "end-to-end encrypted relay capture",
                        "capture_type": "text",
                        "metadata": {"source": "relay_verifier"},
                    }
                ).encode("utf-8")
            ).decode("ascii"),
        }
        command = crypto.seal(
            key=phone_keys.send_key,
            sender_device_id=pairing["device_id"],
            recipient_device_id=pairing["windows_device_id"],
            event_kind="companion_command",
            sequence_number=1,
            payload=command_payload,
        )
        relay_client.upload(relay_phone["token"], command)
        assert bridge.poll_once()["processed"] == 1
        response = relay_client.pull(relay_phone["token"])["items"][0]
        opened = crypto.open(
            key=phone_keys.receive_key,
            envelope=response,
            expected_sender=pairing["windows_device_id"],
            expected_recipient=pairing["device_id"],
        )
        assert opened["request_message_id"] == command["message_id"]
        assert opened["status_code"] == 201
        capture_result = json.loads(base64.b64decode(opened["body"]))
        assert (
            capture_result["created_memory"]["text"]
            == "end-to-end encrypted relay capture"
        )

        retry = crypto.seal(
            key=phone_keys.send_key,
            sender_device_id=pairing["device_id"],
            recipient_device_id=pairing["windows_device_id"],
            event_kind="companion_command",
            sequence_number=2,
            payload=command_payload,
        )
        relay_client.upload(relay_phone["token"], retry)
        assert bridge.poll_once()["redelivered"] == 1
        matching_memories = [
            memory
            for memory in app.memory.list_all(limit=20)
            if memory["text"] == "end-to-end encrypted relay capture"
        ]
        assert len(matching_memories) == 1
        app.shutdown_services()

    print("Phase 2 relay verification passed.")


if __name__ == "__main__":
    main()
