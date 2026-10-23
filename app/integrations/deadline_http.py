"""Opt-in synchronous HTTP deadline for owner query embeddings.

HTTPX's timeout is per I/O wait. A ContextVar lets every operation on a pooled
connection use the remaining *total* budget without sharing turn state or starting
a provider thread. Untimed model calls retain normal HTTPX behavior.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

import httpcore
import httpx

_DEADLINE: ContextVar[float | None] = ContextVar("embedding_http_deadline", default=None)


@contextmanager
def embedding_http_deadline(timeout: float):
    parent = _DEADLINE.get()
    deadline = monotonic() + timeout
    token = _DEADLINE.set(min(parent, deadline) if parent is not None else deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def _timeout(configured: float | None) -> float | None:
    deadline = _DEADLINE.get()
    if deadline is None:
        return configured
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise httpcore.ReadTimeout("embedding deadline exceeded")
    return min(configured, remaining) if configured is not None else remaining


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(self, stream: httpcore.NetworkStream):
        self._stream = stream

    def read(self, max_bytes, timeout=None):
        result = self._stream.read(max_bytes, timeout=_timeout(timeout))
        _timeout(None)
        return result

    def write(self, buffer, timeout=None):
        self._stream.write(buffer, timeout=_timeout(timeout))
        _timeout(None)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        stream = self._stream.start_tls(
            ssl_context, server_hostname=server_hostname, timeout=_timeout(timeout)
        )
        try:
            _timeout(None)
        except httpcore.TimeoutException:
            stream.close()
            raise
        return _DeadlineStream(stream)

    def close(self):
        self._stream.close()

    def get_extra_info(self, info):
        return self._stream.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, backend: httpcore.NetworkBackend):
        self._backend = backend

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = self._backend.connect_tcp(
            host, port, timeout=_timeout(timeout), local_address=local_address,
            socket_options=socket_options,
        )
        try:
            _timeout(None)
        except httpcore.TimeoutException:
            stream.close()
            raise
        return _DeadlineStream(stream)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return _DeadlineStream(self._backend.connect_unix_socket(
            path, timeout=_timeout(timeout), socket_options=socket_options,
        ))

    def sleep(self, seconds):
        self._backend.sleep(seconds)


def enable_http_deadlines(client: httpx.Client) -> None:
    """Prepare a newly owned client before publishing it to other threads.

    Preserve HTTPX's normal environment-proxy mounts. HTTPX 0.28 exposes no backend
    parameter; keep the private integration seam here and test the actual sockets.
    Injected test transports remain responsible for honoring their timeout contract.
    """
    for transport in {client._transport, *client._mounts.values()}:
        if isinstance(transport, httpx.HTTPTransport):
            pool = transport._pool
            pool._network_backend = _DeadlineBackend(pool._network_backend)


def supports_http_deadlines(client: httpx.Client) -> bool:
    """An injected real transport must be prepared before any connection is opened.

    Custom/mock transports own their timeout semantics. Never mutate an injected
    real client's pool here: it may already have unwrapped keepalive connections.
    """
    return all(
        not isinstance(transport, httpx.HTTPTransport)
        or isinstance(transport._pool._network_backend, _DeadlineBackend)
        for transport in {client._transport, *client._mounts.values()}
    )
