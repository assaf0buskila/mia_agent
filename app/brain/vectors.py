"""Portable embedding storage and exact similarity search.

Vectors are stored as base64 of a little-endian float32 array in a plain TEXT column,
so the schema is byte-identical on SQLite (tests) and RDS PostgreSQL (prod) and needs
no dialect branching and no `POSTGRES_ONLY` entry.

pgvector is deliberately not used. Its SQLAlchemy type is PostgreSQL-dialect-only, which
would mean the test suite exercises a different retrieval path than production. pgvector
also documents that exact search "provides perfect recall" and that an index trades recall
for speed — at this corpus size (single owner, a few thousand rows) there is no speed
problem to trade recall for. See docs/BRAIN_ARCHITECTURE.md and ADR-026.

Vectors are L2-normalized on write, so cosine similarity is a plain dot product.
Measured: 3000 x 1536 exact search is ~170 ms in pure stdlib, against an LLM call that
costs seconds. No numpy dependency is added.
"""

from __future__ import annotations

import base64
import math
import sys
from array import array
from operator import mul

# float32, little-endian on the wire. `array('f')` is native-endian, so big-endian hosts
# byteswap on both encode and decode to keep stored blobs portable across architectures.
_TYPECODE = "f"
_BYTES_PER_FLOAT = 4
_IS_BIG_ENDIAN = sys.byteorder == "big"

MAX_DIM = 4096


class VectorError(ValueError):
    """Raised when a stored vector cannot be decoded or has the wrong shape."""


def l2_normalize(values: list[float]) -> list[float]:
    """Scale to unit length so cosine similarity reduces to a dot product.

    A zero vector is returned unchanged; it scores 0.0 against everything, which is the
    correct behaviour for an empty or failed embedding.
    """
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0.0 or not math.isfinite(norm):
        return [0.0] * len(values)
    return [value / norm for value in values]


def encode_vector(values: list[float]) -> str:
    """Serialize a float vector to base64 float32 for a portable TEXT column."""
    if not values:
        raise VectorError("refusing to encode an empty vector")
    if len(values) > MAX_DIM:
        raise VectorError(f"vector dimension {len(values)} exceeds {MAX_DIM}")
    for value in values:
        if not math.isfinite(value):
            raise VectorError("vector contains a non-finite value")
    buffer = array(_TYPECODE, values)
    if _IS_BIG_ENDIAN:
        buffer.byteswap()
    return base64.b64encode(buffer.tobytes()).decode("ascii")


def decode_vector(blob: str) -> array:
    """Decode a stored vector. Returns `array('f')` for cheap C-level arithmetic."""
    if not blob:
        raise VectorError("cannot decode an empty vector blob")
    try:
        raw = base64.b64decode(blob, validate=True)
    except (ValueError, TypeError) as exc:
        raise VectorError("vector blob is not valid base64") from exc
    if len(raw) % _BYTES_PER_FLOAT:
        raise VectorError("vector blob length is not a multiple of 4")
    buffer = array(_TYPECODE)
    buffer.frombytes(raw)
    if _IS_BIG_ENDIAN:
        buffer.byteswap()
    return buffer


def dot(left: array, right: array) -> float:
    """Dot product. On L2-normalized vectors this is cosine similarity."""
    if len(left) != len(right):
        raise VectorError(f"dimension mismatch: {len(left)} vs {len(right)}")
    return float(sum(map(mul, left, right)))


def cosine_similarity(left: list[float] | array, right: list[float] | array) -> float:
    """Cosine similarity for vectors that may not be normalized. Used in tests/tools."""
    left_arr = left if isinstance(left, array) else array(_TYPECODE, left)
    right_arr = right if isinstance(right, array) else array(_TYPECODE, right)
    if len(left_arr) != len(right_arr):
        raise VectorError(f"dimension mismatch: {len(left_arr)} vs {len(right_arr)}")
    left_norm = math.sqrt(dot(left_arr, left_arr))
    right_norm = math.sqrt(dot(right_arr, right_arr))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot(left_arr, right_arr) / (left_norm * right_norm)


def rank_by_similarity(
    query: list[float] | array,
    candidates: list[tuple[str, str]],
    *,
    limit: int,
) -> list[tuple[str, float]]:
    """Exact nearest-neighbour over `(row_id, encoded_vector)` pairs.

    Rows whose vector fails to decode or has the wrong dimension are skipped rather than
    raising, so one poisoned row cannot take down retrieval. Ties break on row id so the
    ordering is deterministic for tests.
    """
    if limit <= 0 or not candidates:
        return []
    query_arr = query if isinstance(query, array) else array(_TYPECODE, query)
    if not len(query_arr):
        return []
    scored: list[tuple[str, float]] = []
    for row_id, blob in candidates:
        try:
            vector = decode_vector(blob)
        except VectorError:
            continue
        if len(vector) != len(query_arr):
            continue
        scored.append((row_id, dot(query_arr, vector)))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:limit]
