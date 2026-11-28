#!/usr/bin/env python3
"""Same-port TLS/HTTP front for the AgentControl panel.

Caddy answers plaintext HTTP sent to its TLS port with a 400 and the browser
shows an unfriendly error. This front sits on the public port and looks at the
first byte of every connection:

* TLS ClientHello (0x16) -> relayed byte-for-byte to Caddy on loopback.
* Plain HTTP for a hostname -> answered with a 308 redirect to the HTTPS URL.
* Plain HTTP for a bare IP -> proxied to Flask, because ACME cannot issue a
  certificate for an IP address.

Only the standard library is used so it can run from any Python 3 install.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import sys
from urllib.parse import urlsplit

TLS_HANDSHAKE_BYTE = 0x16
MAX_HEADER_BYTES = 16384


def is_ip_literal(host: str) -> bool:
    value = strip_port(host).strip("[]")
    try:
        ipaddress.ip_address(value.split("%", 1)[0])
        return True
    except ValueError:
        return False


def strip_port(host: str) -> str:
    host = host.strip()
    if not host:
        return host
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end > 0 else host
    if host.count(":") == 1:
        name, port = host.rsplit(":", 1)
        if port.isdigit():
            return name
    return host


def request_target(line: bytes) -> str:
    try:
        parts = line.decode("latin-1").split()
    except Exception:
        return "/"
    if len(parts) >= 2:
        target = parts[1]
        if target.startswith(("http://", "https://")):
            parsed = urlsplit(target)
            return parsed.path + (("?" + parsed.query) if parsed.query else "")
        if target.startswith("/"):
            return target
    return "/"


async def read_headers(reader: asyncio.StreamReader, initial: bytes) -> bytes:
    data = initial
    while b"\r\n\r\n" not in data and len(data) < MAX_HEADER_BYTES:
        chunk = await reader.read(4096)
        if not chunk:
            break
        data += chunk
    return data


def build_redirect(head: bytes, host: str, public_port: int) -> bytes:
    lines = head.split(b"\r\n")
    target = request_target(lines[0]) if lines else "/"
    location = f"https://{strip_port(host) or 'localhost'}:{public_port}{target}"
    body = f"Redirecting to {location}\n".encode("utf-8")
    response = (
        "HTTP/1.1 308 Permanent Redirect\r\n"
        f"Location: {location}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1") + body
    return response


def host_header(head: bytes) -> str:
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"host:"):
            return line.split(b":", 1)[1].decode("latin-1").strip()
    return ""


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()


async def relay_stream(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    initial: bytes,
    upstream_host: str,
    upstream_port: int,
) -> None:
    up_reader, up_writer = await asyncio.open_connection(upstream_host, upstream_port)
    up_writer.write(initial)
    await up_writer.drain()
    tasks = {
        asyncio.create_task(pipe(client_reader, up_writer)),
        asyncio.create_task(pipe(up_reader, client_writer)),
    }
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(Exception):
                task.result()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        with contextlib.suppress(Exception):
            up_writer.close()


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    upstream_host: str,
    upstream_port: int,
    public_port: int,
    default_host: str,
    timeout: float,
    plain_port: int = 0,
) -> None:
    peer = writer.get_extra_info("peername") or ("", 0)
    try:
        initial = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if not initial:
            return
        if initial[0] == TLS_HANDSHAKE_BYTE:
            await relay_stream(reader, writer, initial, upstream_host, upstream_port)
            return
        head = await asyncio.wait_for(read_headers(reader, initial), timeout=timeout)
        if not head:
            return
        host = host_header(head) or default_host or str(peer[0] or "localhost")
        # ACME cannot issue for a bare IP. Let plain-HTTP IP access reach the
        # panel instead of redirecting to an HTTPS URL with no certificate.
        if plain_port and is_ip_literal(host):
            await relay_stream(reader, writer, head, "127.0.0.1", plain_port)
            return
        writer.write(build_redirect(head, host, public_port))
        await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def serve(args: argparse.Namespace) -> None:
    server = await asyncio.start_server(
        lambda r, w: handle_client(
            r,
            w,
            upstream_host=args.upstream_host,
            upstream_port=args.upstream_port,
            public_port=args.public_port,
            default_host=args.default_host,
            timeout=args.timeout,
            plain_port=args.plain_port,
        ),
        args.listen_host,
        args.listen_port,
        reuse_address=True,
    )
    addrs = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    print(
        f"tls_front listening on {addrs}; TLS -> {args.upstream_host}:{args.upstream_port}; "
        f"HTTP -> https://HOST:{args.public_port}",
        flush=True,
    )
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, required=True)
    parser.add_argument("--public-port", type=int, required=True)
    parser.add_argument("--default-host", default="")
    parser.add_argument(
        "--plain-port",
        type=int,
        default=0,
        help="Flask port used for plain-HTTP requests addressed to a bare IP",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
