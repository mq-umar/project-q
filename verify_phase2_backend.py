from __future__ import annotations

import base64
import json
import secrets
import shutil
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from project_q.app import create_application
from project_q.config import AppConfig
from project_q.models import ChatRequest, MemoryCreate, MemoryUpdate, RoutineCreate, utc_now
from project_q.services.companion_crypto import CompanionCryptoService
from project_q.tools.base import ToolDefinition


class VerificationTool:
    definition = ToolDefinition(
        tool_id="verification.publish",
        name="Verification Publish",
        description="records one approved execution",
        tier=3,
    )

    def __init__(self) -> None:
        self.executions: list[dict] = []

    def execute(self, payload: dict) -> dict:
        self.executions.append(dict(payload))
        return {"status": "published", "count": len(self.executions)}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="project-q-phase2-backend-")).resolve()
    try:
        config = AppConfig(
            project_name="Project Q Phase 2 Verification",
            workspace_root=root,
            data_root=root / ".project_q",
            db_path=root / ".project_q" / "project_q.db",
            port=0,
        )
        app = create_application(config)
        crypto = CompanionCryptoService()
        phone_key_pair = crypto.generate_key_pair()
        approval_private_key = ec.generate_private_key(ec.SECP256R1())
        approval_public_key = _b64(
            approval_private_key.public_key().public_bytes(
                encoding=serialization.Encoding.X962,
                format=serialization.PublicFormat.UncompressedPoint,
            )
        )
        pairing = app.companion.start_pairing_v2(
            device_name="Verification iPhone",
            platform="ios",
        )
        completed = app.companion.complete_pairing_v2(
            device_id=pairing["device_id"],
            pairing_token=pairing["pairing_token"],
            key_agreement_public_key=phone_key_pair.public_key,
            approval_public_key=approval_public_key,
        )
        pairing_token_bytes = base64.urlsafe_b64decode(
            pairing["pairing_token"] + ("=" * (-len(pairing["pairing_token"]) % 4))
        )
        phone_keys = crypto.derive_session_keys(
            private_key=phone_key_pair.private_key,
            peer_public_key=pairing["windows_key_agreement_public_key"],
            pairing_token=pairing_token_bytes,
            local_device_id=completed["device_id"],
            remote_device_id=completed["windows_device_id"],
        )

        body = b'{"state":"active","ttl_seconds":120}'
        request_timestamp = utc_now()
        request_signature = app.companion_auth.sign_request(
            request_key=phone_keys.request_key,
            device_id=completed["device_id"],
            method="POST",
            path="/api/companion/v2/presence",
            timestamp=request_timestamp,
            request_nonce="verification-nonce-0001",
            sequence_number=1,
            body=body,
        )
        app.companion_auth.verify_request(
            device_id=completed["device_id"],
            method="POST",
            path="/api/companion/v2/presence",
            timestamp=request_timestamp,
            request_nonce="verification-nonce-0001",
            sequence_number=1,
            body=body,
            signature=request_signature,
        )
        try:
            app.companion_auth.verify_request(
                device_id=completed["device_id"],
                method="POST",
                path="/api/companion/v2/presence",
                timestamp=request_timestamp,
                request_nonce="verification-nonce-0001",
                sequence_number=1,
                body=body,
                signature=request_signature,
            )
            raise AssertionError("replayed companion request was accepted")
        except PermissionError:
            pass

        app.companion.publish_presence(
            completed["device_id"],
            state="active",
            ttl_seconds=120,
        )
        chat = app.conversations.respond(
            ChatRequest(message="hello from phase two verification"),
            channel="iphone",
            input_sources=["owner", "iphone_companion", "reasoner"],
        )
        assert chat.reply
        assert app.conversations.list_messages(limit=1)[0]["channel"] == "iphone"

        memory = app.memory.create(
            MemoryCreate(
                text="verification quick capture",
                kind="episodic",
                source="iphone_companion",
                confidence=0.75,
                owner_confirmed=False,
                tags=["iphone", "quick_capture"],
            )
        )
        updated_memory = app.memory.update(
            memory["id"],
            MemoryUpdate(text="verification quick capture updated"),
        )
        assert updated_memory["text"].endswith("updated")
        routine = app.routines.create(
            RoutineCreate(
                name="Verification Routine",
                goal="Prove remote routine triggering",
                trusted=True,
            )
        )
        assert app.routine_runner.run(
            routine["id"],
            owner_approved=False,
        )["outcome"] == "completed"

        tool = VerificationTool()
        app.tools.register(tool)
        sensitive_value = "phase2-verifier-secret"
        action = app.approvals.create_request(
            tool_id=tool.definition.tool_id,
            action_tier=tool.definition.tier,
            payload={"command": "publish", "api_key": sensitive_value},
            summary="Publish verification result",
            input_sources=["owner"],
        )
        decision_timestamp = utc_now()
        decision_message = app.approvals.decision_message(
            action_request=action,
            decision="approve",
            device_id=completed["device_id"],
            timestamp=decision_timestamp,
        )
        decision_signature = _b64(
            approval_private_key.sign(
                decision_message,
                ec.ECDSA(hashes.SHA256()),
            )
        )
        app.approvals.decide(
            action_request_id=action["id"],
            device_id=completed["device_id"],
            decision="approve",
            timestamp=decision_timestamp,
            signature=decision_signature,
            biometric_backed=True,
        )
        assert app.approvals.execute_approved(action["id"])["status"] == "completed"
        assert app.approvals.execute_approved(action["id"])["status"] == "completed"
        assert len(tool.executions) == 1

        sync = app.sync.pull(completed["device_id"], after_sequence=0, limit=500)
        assert sync["items"]
        app.sync.acknowledge(
            completed["device_id"],
            sequence_number=sync["next_sequence"],
        )
        snapshot = app.sync.snapshot(completed["device_id"])
        assert snapshot["snapshot_sequence"] >= sync["next_sequence"]

        key = secrets.token_bytes(32)
        envelope = crypto.seal(
            key=key,
            sender_device_id="phone",
            recipient_device_id="windows",
            event_kind="verification",
            sequence_number=1,
            payload={"text": "encrypted verification"},
        )
        tampered = {**envelope, "tag": envelope["tag"][:-2] + "AA"}
        try:
            crypto.open(
                key=key,
                envelope=tampered,
                expected_sender="phone",
                expected_recipient="windows",
            )
            raise AssertionError("tampered AES-GCM envelope was accepted")
        except ValueError:
            pass

        database_bytes = config.db_path.read_bytes()
        assert pairing["pairing_token"].encode("utf-8") not in database_bytes
        assert sensitive_value.encode("utf-8") not in database_bytes
        assert sensitive_value not in json.dumps(app.audit.list_recent(limit=100))

        restarted = create_application(config)
        assert restarted.sync.snapshot(completed["device_id"])["device_id"] == completed["device_id"]
        restarted.companion.revoke_device(
            completed["device_id"],
            reason="verification complete",
            source="system",
        )
        try:
            restarted.sync.pull(completed["device_id"], after_sequence=0)
            raise AssertionError("revoked device retained sync access")
        except PermissionError:
            pass

        print("Phase 2 backend verification passed.")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
