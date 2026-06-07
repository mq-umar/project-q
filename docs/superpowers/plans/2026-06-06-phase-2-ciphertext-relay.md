# Phase 2 Ciphertext Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-hostable relay that routes encrypted Project Q envelopes, presence, acknowledgements, revocations, and APNs wake signals without possessing content keys.

**Architecture:** A standalone FastAPI service stores opaque envelopes and routing metadata in its own SQLite database. Device connections authenticate with relay-scoped credentials issued by Windows; content encryption remains end to end. WebSocket is preferred and bounded polling is the fallback.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, Pydantic 2, SQLite, Docker, `unittest`.

**PRD Coverage:** Phase 2 end-to-end relay and push-notification transport, PRD sections 11.2-11.3 and 13.3, plus Phase 4 self-hostable relay foundation.

---

### Task 1: Relay package, schema, and authentication

**Files:**
- Create: `relay/pyproject.toml`
- Create: `relay/project_q_relay/config.py`
- Create: `relay/project_q_relay/storage.py`
- Create: `relay/project_q_relay/auth.py`
- Test: `relay/tests/test_relay.py`

- [ ] Write failing tests for device registration, scoped relay tokens, token hashing, expiry, revocation, and database restart.
- [ ] Run tests and confirm imports fail.
- [ ] Implement configuration and transactional SQLite schema for devices, envelopes, acknowledgements, presence, revocations, and APNs routes.
- [ ] Implement constant-time bearer-token verification with only token hashes stored.
- [ ] Run tests and confirm all authentication cases pass.

### Task 2: Opaque envelope inbox

**Files:**
- Create: `relay/project_q_relay/service.py`
- Test: `relay/tests/test_relay.py`

- [ ] Write failing tests that upload arbitrary ciphertext, pull only the intended recipient inbox, acknowledge idempotently, expire messages, and reject oversized envelopes.
- [ ] Assert the relay never imports Project Q content crypto, accepts no plaintext payload field, and logs no ciphertext or APNs token.
- [ ] Implement bounded envelope upload/pull/acknowledgement and retention cleanup.
- [ ] Run tests including concurrent duplicate upload with one stored message.

### Task 3: Presence, revocation, and WebSocket delivery

**Files:**
- Create: `relay/project_q_relay/app.py`
- Test: `relay/tests/test_relay.py`

- [ ] Write failing ASGI tests for health, presence expiry, revocation fan-out, WebSocket delivery, reconnect, and polling fallback.
- [ ] Implement FastAPI routes and authenticated WebSocket sessions.
- [ ] Ensure revocation closes active sockets and blocks all later traffic.
- [ ] Run ASGI and concurrency tests.

### Task 4: APNs adapter

**Files:**
- Create: `relay/project_q_relay/apns.py`
- Test: `relay/tests/test_relay.py`

- [ ] Write failing tests with a recording adapter proving only message kind, opaque routing ID, and collapse ID reach APNs.
- [ ] Implement disabled, recording, and production HTTP/2 adapter interfaces; production credentials come only from environment or mounted secret files.
- [ ] Reject APNs payload content, ciphertext, and owner data by schema.
- [ ] Run adapter and secret-leak tests.

### Task 5: Container and behavioral simulation

**Files:**
- Create: `relay/Dockerfile`
- Create: `relay/docker-compose.yml`
- Create: `relay/.env.example`
- Create: `relay/README.md`
- Create: `verify_phase2_relay.py`

- [ ] Write a verifier that starts the ASGI app in process and simulates Windows/phone delivery, offline queueing, reconnect, acknowledgement, presence, expiry, and revocation.
- [ ] Add Docker healthcheck, non-root user, read-only root filesystem support, resource limits, and persistent data volume.
- [ ] Run relay tests, verifier, dependency audit when available, and container configuration validation.
- [ ] Update the PRD matrix with exact relay evidence and keep live APNs delivery as an owner-credential environment gate.
