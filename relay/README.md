# Project Q Ciphertext Relay

The relay is a self-hostable routing service for Project Q protocol-v2
envelopes. It never receives companion content keys and rejects payload fields
outside the opaque AES-GCM envelope schema.

## Security Model

- bearer credentials are random and stored only as SHA-256 hashes
- devices may exchange messages only within one owner scope
- envelope bodies remain X25519/HKDF/AES-256-GCM ciphertext
- duplicate message IDs are idempotent only when the complete envelope matches
- presence expires after at most 120 seconds
- revocation immediately blocks HTTP polling and WebSocket delivery
- APNs wake adapters receive only message kind and opaque routing identifiers
- the container runs as UID 10001 with all capabilities dropped

Device registration is intentionally not exposed as a public relay endpoint.
The hidden provisioning endpoint requires the owner bootstrap credential.
Project Q stores the Windows relay token in DPAPI Vault and transfers only the
phone-scoped token through the short-lived local pairing payload.

## Run

```powershell
Copy-Item relay/.env.example relay/.env
# Replace PROJECT_Q_RELAY_BOOTSTRAP_TOKEN with a long random value.
# Set PROJECT_Q_RELAY_APNS_ROUTE_KEY to URL-safe base64 for 32 random bytes.
docker compose -f relay/docker-compose.yml up --build
```

The health endpoint is `http://127.0.0.1:8080/health`. Production deployments
must put TLS and rate limiting in front of the service.

Configure the Windows runtime with the same relay URL and bootstrap value:

```powershell
$env:PROJECT_Q_RELAY_URL = "https://relay.example.com"
$env:PROJECT_Q_RELAY_BOOTSTRAP_TOKEN = "<same-bootstrap-token>"
```

## Verify

```powershell
$env:PYTHONPATH='relay'
python -m unittest relay.tests.test_relay
python verify_phase2_relay.py
```

Live APNs delivery remains an environment gate until the owner supplies an
Apple Developer key, bundle identifier, and a real device token.

When those credentials are present, the relay uses APNs token authentication
over HTTP/2. Device tokens are encrypted at rest with
`PROJECT_Q_RELAY_APNS_ROUTE_KEY`; push payloads carry only generic wake or
approval metadata and an opaque routing identifier.
