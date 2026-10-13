"""Small CONNECT-only proxy for the two official Apps Script HTTPS hosts."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import sys

ALLOWED = frozenset({"script.google.com", "script.googleusercontent.com"})
BIND = "172.29.22.3"
PORT = 8080
MAX_HEADER = 4096
MAX_STREAM = 2 * 1024 * 1024
CONNECT_TIMEOUT = 5
IDLE_TIMEOUT = 20
TOTAL_TIMEOUT = 40


async def public_addresses(host: str) -> tuple[str, ...]:
    if host not in ALLOWED:
        raise ValueError("proxy_host_denied")
    rows = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM),
        CONNECT_TIMEOUT,
    )
    addresses = tuple(dict.fromkeys(row[4][0] for row in rows))
    if not addresses:
        raise ValueError("proxy_dns_empty")
    for value in addresses:
        ip = ipaddress.ip_address(value)
        if (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
            or value == "169.254.169.254"
        ):
            raise ValueError("proxy_address_denied")
    return addresses


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    total = 0
    while True:
        data = await asyncio.wait_for(reader.read(16384), IDLE_TIMEOUT)
        if not data:
            return
        total += len(data)
        if total > MAX_STREAM:
            raise ValueError("proxy_stream_limit")
        writer.write(data)
        await asyncio.wait_for(writer.drain(), IDLE_TIMEOUT)


async def _tunnel(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    upstream = None
    code = "proxy_rejected"
    try:
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), IDLE_TIMEOUT)
        if len(header) > MAX_HEADER:
            raise ValueError("proxy_header_limit")
        lines = header.split(b"\r\n")
        if len(lines) < 2 or lines[0].count(b" ") != 2:
            raise ValueError("proxy_request_invalid")
        method, authority, version = lines[0].decode("ascii").split(" ")
        if method != "CONNECT" or version != "HTTP/1.1" or authority.count(":") != 1:
            raise ValueError("proxy_connect_required")
        host, port = authority.rsplit(":", 1)
        if port != "443" or host != host.lower() or host not in ALLOWED:
            raise ValueError("proxy_target_denied")
        for line in lines[1:]:
            if not line:
                continue
            name, sep, _ = line.partition(b":")
            if not sep or name.lower() not in {b"host", b"proxy-connection", b"connection"}:
                raise ValueError("proxy_header_denied")
        addresses = await public_addresses(host)
        last_error = None
        for address in addresses:
            try:
                upstream = await asyncio.wait_for(
                    asyncio.open_connection(address, 443), CONNECT_TIMEOUT
                )
                break
            except (OSError, TimeoutError) as exc:
                last_error = exc
        if upstream is None:
            raise last_error or OSError("connect_failed")
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_relay(reader, upstream[1]), _relay(upstream[0], writer))
        code = "proxy_complete"
    except (
        ValueError,
        UnicodeError,
        OSError,
        TimeoutError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
    ):
        if not writer.is_closing():
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
    finally:
        print(code, file=sys.stderr)
        if upstream:
            upstream[1].close()
        writer.close()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    # One deadline includes headers, DNS, every connect attempt and both streams.
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            await _tunnel(reader, writer)
    except TimeoutError:
        # _tunnel's finally closes both sockets before this safe reason code.
        print("proxy_deadline", file=sys.stderr)


async def main() -> None:
    server = await asyncio.start_server(handle, BIND, PORT, limit=MAX_HEADER)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
