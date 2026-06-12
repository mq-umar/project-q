from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from project_q.models import utc_now


PROTOCOL_VERSION = 2
ALGORITHM = "X25519-HKDF-SHA256-AES-256-GCM"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    text = str(value)
    padding = "=" * (-len(text) % 4)
    return base64.b64decode(
        (text + padding).encode("ascii"),
        altchars=b"-_",
        validate=True,
    )


@dataclass(frozen=True, slots=True)
class CompanionKeyPair:
    private_key: X25519PrivateKey
    public_key: str


@dataclass(frozen=True, slots=True)
class CompanionSessionKeys:
    send_key: bytes
    receive_key: bytes
    request_key: bytes


class CompanionCryptoService:
    def generate_key_pair(self) -> CompanionKeyPair:
        private_key = X25519PrivateKey.generate()
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return CompanionKeyPair(private_key=private_key, public_key=_b64encode(public_bytes))

    def serialize_private_key(self, private_key: X25519PrivateKey) -> str:
        raw = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return _b64encode(raw)

    def load_private_key(self, encoded_private_key: str) -> X25519PrivateKey:
        raw = _b64decode(encoded_private_key)
        if len(raw) != 32:
            raise ValueError("invalid X25519 private key")
        return X25519PrivateKey.from_private_bytes(raw)

    def derive_session_keys(
        self,
        *,
        private_key: X25519PrivateKey,
        peer_public_key: str,
        pairing_token: bytes,
        local_device_id: str,
        remote_device_id: str,
    ) -> CompanionSessionKeys:
        local_id = str(local_device_id).strip()
        remote_id = str(remote_device_id).strip()
        if not local_id or not remote_id or local_id == remote_id:
            raise ValueError("companion key derivation requires two distinct device IDs")
        if len(pairing_token) < 32:
            raise ValueError("pairing token must contain at least 256 bits")

        peer_bytes = _b64decode(peer_public_key)
        if len(peer_bytes) != 32:
            raise ValueError("invalid X25519 public key")
        shared_secret = private_key.exchange(X25519PublicKey.from_public_bytes(peer_bytes))
        first_id, second_id = sorted((local_id, remote_id))
        salt = hashlib.sha256(
            b"PROJECTQ-COMPANION-PAIRING-V2\0" + pairing_token
        ).digest()
        info = (
            "PROJECTQ-COMPANION-SESSION-V2\n"
            f"{first_id}\n"
            f"{second_id}\n"
        ).encode("utf-8")
        material = HKDF(
            algorithm=hashes.SHA256(),
            length=96,
            salt=salt,
            info=info,
        ).derive(shared_secret)
        first_to_second = material[:32]
        second_to_first = material[32:64]
        request_key = material[64:]
        if local_id == first_id:
            send_key = first_to_second
            receive_key = second_to_first
        else:
            send_key = second_to_first
            receive_key = first_to_second
        return CompanionSessionKeys(
            send_key=send_key,
            receive_key=receive_key,
            request_key=request_key,
        )

    def seal(
        self,
        *,
        key: bytes,
        sender_device_id: str,
        recipient_device_id: str,
        event_kind: str,
        sequence_number: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._validate_key(key)
        if not isinstance(payload, dict):
            raise ValueError("companion payload must be a JSON object")
        metadata = {
            "protocol_version": PROTOCOL_VERSION,
            "message_id": f"msg_{secrets.token_hex(16)}",
            "sender_device_id": self._required_text(sender_device_id, "sender device ID"),
            "recipient_device_id": self._required_text(recipient_device_id, "recipient device ID"),
            "event_kind": self._required_text(event_kind, "event kind"),
            "created_at": utc_now(),
            "sequence_number": self._sequence(sequence_number),
        }
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encrypted = AESGCM(key).encrypt(nonce, plaintext, self._aad(metadata))
        ciphertext, tag = encrypted[:-16], encrypted[-16:]
        return {
            **metadata,
            "nonce": _b64encode(nonce),
            "ciphertext": _b64encode(ciphertext),
            "tag": _b64encode(tag),
            "content_type": "application/json",
            "algorithm": ALGORITHM,
        }

    def open(
        self,
        *,
        key: bytes,
        envelope: dict[str, Any],
        expected_sender: str,
        expected_recipient: str,
    ) -> dict[str, Any]:
        self._validate_key(key)
        try:
            metadata = {
                "protocol_version": int(envelope["protocol_version"]),
                "message_id": self._required_text(envelope["message_id"], "message ID"),
                "sender_device_id": self._required_text(
                    envelope["sender_device_id"], "sender device ID"
                ),
                "recipient_device_id": self._required_text(
                    envelope["recipient_device_id"], "recipient device ID"
                ),
                "event_kind": self._required_text(envelope["event_kind"], "event kind"),
                "created_at": self._required_text(envelope["created_at"], "creation timestamp"),
                "sequence_number": self._sequence(envelope["sequence_number"]),
            }
            if metadata["protocol_version"] != PROTOCOL_VERSION:
                raise ValueError("unsupported companion protocol version")
            if metadata["sender_device_id"] != expected_sender:
                raise ValueError("unexpected companion envelope sender")
            if metadata["recipient_device_id"] != expected_recipient:
                raise ValueError("unexpected companion envelope recipient")
            if envelope.get("algorithm") != ALGORITHM:
                raise ValueError("unsupported companion envelope algorithm")

            nonce = _b64decode(str(envelope["nonce"]))
            ciphertext = _b64decode(str(envelope["ciphertext"]))
            tag = _b64decode(str(envelope["tag"]))
            if len(nonce) != 12 or len(tag) != 16:
                raise ValueError("invalid companion envelope nonce or tag")
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext + tag,
                self._aad(metadata),
            )
            payload = json.loads(plaintext.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("companion payload must be a JSON object")
            return payload
        except (InvalidTag, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid companion envelope") from exc

    @staticmethod
    def _aad(metadata: dict[str, Any]) -> bytes:
        return json.dumps(
            metadata,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _required_text(value: Any, label: str) -> str:
        text = str(value).strip()
        if not text or len(text) > 500:
            raise ValueError(f"invalid {label}")
        return text

    @staticmethod
    def _sequence(value: Any) -> int:
        sequence = int(value)
        if sequence < 1 or sequence > 9_223_372_036_854_775_807:
            raise ValueError("invalid companion sequence number")
        return sequence

    @staticmethod
    def _validate_key(key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
