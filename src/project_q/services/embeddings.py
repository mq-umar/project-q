"""Best-effort semantic embeddings for memory search.

Design constraints (hard):
- stdlib only — the bundled runtime has cryptography + pydantic only (no
  requests/numpy). All HTTP uses urllib; cosine similarity is pure Python.
- Degrade silently — this environment has NO Ollama running. Any failure
  (connection refused, timeout, malformed payload, provider disabled) returns
  None so callers fall back to FTS5/LIKE lexical search. Tests must NOT require
  a live Ollama.

The embedding endpoint is the sibling of the chat endpoint configured in
settings ("model_base_url", default http://localhost:11434/api/chat). We reuse
the same urllib POST pattern as the reasoner and derive /api/embeddings from the
base URL the same way the reasoner derives /api/tags.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable, Optional
from urllib import error, request
from urllib.parse import urlsplit, urlunsplit


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Pure-Python cosine similarity. Returns 0.0 for degenerate inputs."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


class EmbeddingService:
    """Computes embedding vectors via Ollama's /api/embeddings, best-effort.

    A custom ``transport`` callable (url, body_bytes, headers, timeout) -> dict
    can be injected for tests so the hybrid-rerank path is exercisable without a
    live Ollama process.
    """

    def __init__(
        self,
        settings_service=None,
        transport: Optional[Callable[[str, bytes, dict[str, str], int], dict[str, Any]]] = None,
    ) -> None:
        self._settings_service = settings_service
        self._transport = transport or self._default_transport

    # ── public API ──────────────────────────────────────────────────────────
    def is_enabled(self) -> bool:
        """Embeddings ride on the same provider toggle as the reasoner."""
        settings = self._settings()
        return bool(settings.get("provider_enabled", False)) and str(
            settings.get("provider_type", "ollama")
        ).lower() == "ollama"

    def embed(self, text: str) -> Optional[list[float]]:
        """Return an embedding vector, or None on any failure (silent fallback)."""
        normalized = (text or "").strip()
        if not normalized:
            return None
        if not self.is_enabled():
            return None
        settings = self._settings()
        url = self._embeddings_url(str(settings.get("model_base_url", "")))
        model = str(
            settings.get("ollama_general_model")
            or settings.get("model_name")
            or "nomic-embed-text"
        )
        timeout = int(settings.get("provider_timeout_seconds", 120) or 120)
        body = json.dumps({"model": model, "prompt": normalized}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        try:
            payload = self._transport(url, body, headers, timeout)
        except Exception:  # noqa: BLE001 — any transport error → lexical fallback
            return None
        return self._parse_vector(payload)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _settings(self) -> dict[str, Any]:
        if self._settings_service is None:
            return {}
        try:
            return self._settings_service.get_all()
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _parse_vector(payload: Any) -> Optional[list[float]]:
        if not isinstance(payload, dict):
            return None
        # Ollama returns {"embedding": [...]}; some variants use {"embeddings": [[...]]}
        vector = payload.get("embedding")
        if vector is None:
            nested = payload.get("embeddings")
            if isinstance(nested, list) and nested and isinstance(nested[0], list):
                vector = nested[0]
        if not isinstance(vector, list) or not vector:
            return None
        try:
            return [float(value) for value in vector]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _embeddings_url(base_url: str) -> str:
        """Derive the /api/embeddings sibling of the configured chat endpoint."""
        parsed = urlsplit(base_url or "http://localhost:11434/api/chat")
        path = parsed.path or "/api/chat"
        if "/api/" in path:
            prefix = path.split("/api/", 1)[0]
            path = prefix + "/api/embeddings"
        else:
            path = "/api/embeddings"
        netloc = parsed.netloc or "localhost:11434"
        scheme = parsed.scheme or "http"
        return urlunsplit((scheme, netloc, path, "", ""))

    @staticmethod
    def _default_transport(
        url: str, body: bytes, headers: dict[str, str], timeout_seconds: int
    ) -> dict[str, Any]:
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Embedding request failed: {exc.code} {details}") from exc
