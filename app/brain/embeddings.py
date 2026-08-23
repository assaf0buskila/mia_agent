"""Embedding port.

OpenAI `POST /v1/embeddings` behind a typed port, with the Gemini OpenAI-compatibility
endpoint as an explicitly-selected alternative. Default runtime is Disabled: with no key
configured the brain degrades to keyword-only retrieval instead of failing, which is what
keeps the test suite and a key-less deployment green.

Never mix vectors from two models in one table — cosine across model families is
meaningless. The provider is therefore chosen by an explicit `MIA_EMBEDDING_PROVIDER`
setting, never by automatic failover, and every row stores the model and dimension it was
written with so a model change is a detectable backfill rather than silent corruption.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from app.brain.vectors import l2_normalize
from app.core.config import Settings
from app.core.errors import MiaError

_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
_GEMINI_EMBEDDINGS_URL = "https://generativelanguage.googleapis.com/v1beta/openai/embeddings"

# Documented request limits: max 2048 items per array, 8192 tokens per input,
# 300000 tokens summed across a single request.
MAX_BATCH_ITEMS = 128
MAX_INPUT_CHARS = 8000
_TIMEOUT = 30.0


class EmbeddingError(MiaError):
    code = "embedding_failed"
    http_status = 502


class EmbeddingPort(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def enabled(self) -> bool: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _clip(text: str) -> str:
    """Embeddings reject empty strings; a single space keeps batch indices aligned."""
    cleaned = text.strip()
    if not cleaned:
        return " "
    return cleaned[:MAX_INPUT_CHARS]


class DisabledEmbeddingPort:
    """No embedding provider configured. Retrieval falls back to keyword scoring."""

    @property
    def model(self) -> str:
        return ""

    @property
    def dim(self) -> int:
        return 0

    def enabled(self) -> bool:
        return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        del texts
        return []


class FakeEmbeddingPort:
    """Deterministic test double.

    Produces a stable unit vector from a character histogram, so semantically similar
    strings that share tokens score higher than unrelated ones. Good enough to exercise
    ranking, dedup and supersession without a network call.
    """

    def __init__(self, *, dim: int = 64, model: str = "fake-embedding") -> None:
        self._dim = dim
        self._model = model
        self.calls = 0
        self.embedded: list[str] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def enabled(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        vectors: list[list[float]] = []
        for text in texts:
            self.embedded.append(text)
            buckets = [0.0] * self._dim
            for token in _tokenize(text):
                buckets[hash_token(token) % self._dim] += 1.0
            if not any(buckets):
                buckets[0] = 1.0
            vectors.append(l2_normalize(buckets))
        return vectors


def hash_token(token: str) -> int:
    """Stable across processes, unlike `hash()` with PYTHONHASHSEED randomization."""
    value = 2166136261
    for char in token:
        value = ((value ^ ord(char)) * 16777619) & 0xFFFFFFFF
    return value


def _tokenize(text: str) -> list[str]:
    cleaned = "".join(char if char.isalnum() else " " for char in text.lower())
    return [token for token in cleaned.split() if token]


class OpenAIEmbeddingPort:
    """Live adapter for OpenAI-shaped `/embeddings` (OpenAI and Gemini compat)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dim: int,
        url: str = _OPENAI_EMBEDDINGS_URL,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dim = dim
        self._url = url
        self._client = client

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def enabled(self) -> bool:
        return bool(self._api_key and self._model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts or not self.enabled():
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_BATCH_ITEMS):
            batch = [_clip(text) for text in texts[start : start + MAX_BATCH_ITEMS]]
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload: dict[str, object] = {
            "model": self._model,
            "input": batch,
            "encoding_format": "float",
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._client is not None:
                response = self._client.post(self._url, json=payload, headers=headers)
            else:
                with httpx.Client(timeout=_TIMEOUT) as client:
                    response = client.post(self._url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise EmbeddingError("embedding request failed") from exc
        if response.status_code >= 400:
            raise EmbeddingError(f"embedding request failed: HTTP {response.status_code}")
        return self._parse(response, expected=len(batch))

    def _parse(self, response: httpx.Response, *, expected: int) -> list[list[float]]:
        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingError("embedding response was not JSON") from exc
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != expected:
            raise EmbeddingError("embedding response had an unexpected shape")
        # The API documents that results carry an `index`; do not assume input order.
        ordered: list[list[float]] = [[] for _ in range(expected)]
        for item in data:
            if not isinstance(item, dict):
                raise EmbeddingError("embedding response item was not an object")
            vector = item.get("embedding")
            index = item.get("index", 0)
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("embedding response item had no vector")
            if not isinstance(index, int) or not 0 <= index < expected:
                raise EmbeddingError("embedding response item had a bad index")
            ordered[index] = l2_normalize([float(value) for value in vector])
        if any(not vector for vector in ordered):
            raise EmbeddingError("embedding response was missing an index")
        return ordered


def build_embedding_port(settings: Settings) -> EmbeddingPort:
    """Explicit provider selection. Never fails over between model families."""
    model = settings.embedding_model.strip()
    dim = settings.embedding_dim
    if not model or dim <= 0:
        return DisabledEmbeddingPort()
    provider = settings.embedding_provider.strip().lower()
    if provider == "gemini":
        key = settings.gemini_api_key.strip()
        if not key:
            return DisabledEmbeddingPort()
        return OpenAIEmbeddingPort(
            api_key=key, model=model, dim=dim, url=_GEMINI_EMBEDDINGS_URL
        )
    key = settings.openai_api_key.strip()
    if not key:
        return DisabledEmbeddingPort()
    return OpenAIEmbeddingPort(api_key=key, model=model, dim=dim)
