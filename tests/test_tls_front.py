import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tls_front  # noqa: E402


class RedirectTests(unittest.TestCase):
    def test_build_redirect_uses_host_and_public_port(self):
        response = tls_front.build_redirect(
            b"GET /oc/?x=1 HTTP/1.1\r\nHost: panel.example.com:30228\r\n\r\n",
            "panel.example.com:30228",
            30228,
        )
        self.assertIn(b"HTTP/1.1 308", response)
        self.assertIn(b"Location: https://panel.example.com:30228/oc/?x=1", response)

    def test_build_redirect_host_without_port(self):
        response = tls_front.build_redirect(
            b"GET /health HTTP/1.1\r\nHost: panel.example.com\r\n\r\n",
            "panel.example.com",
            30228,
        )
        self.assertIn(b"Location: https://panel.example.com:30228/health", response)

    def test_build_redirect_ipv6_host(self):
        self.assertEqual(tls_front.strip_port("[2001:db8::1]:30228"), "[2001:db8::1]")
        response = tls_front.build_redirect(
            b"GET / HTTP/1.1\r\nHost: [2001:db8::1]:30228\r\n\r\n",
            "[2001:db8::1]:30228",
            30228,
        )
        self.assertIn(b"Location: https://[2001:db8::1]:30228/", response)

    def test_absolute_form_target_is_reduced_to_path(self):
        response = tls_front.build_redirect(
            b"GET http://panel.example.com/health?x=1 HTTP/1.1\r\nHost: panel.example.com\r\n\r\n",
            "panel.example.com",
            30228,
        )
        self.assertIn(b"Location: https://panel.example.com:30228/health?x=1", response)


class HandleClientTests(unittest.TestCase):
    def test_http_request_gets_redirect(self):
        async def run():
            server = await asyncio.start_server(
                lambda r, w: tls_front.handle_client(
                    r,
                    w,
                    upstream_host="127.0.0.1",
                    upstream_port=1,
                    public_port=30228,
                    default_host="panel.example.com",
                    timeout=2,
                ),
                "127.0.0.1",
                0,
            )
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /health HTTP/1.1\r\nHost: panel.example.com\r\n\r\n")
            await writer.drain()
            data = await reader.read()
            writer.close()
            server.close()
            await server.wait_closed()
            return data

        data = asyncio.run(run())
        self.assertIn(b"308 Permanent Redirect", data)
        self.assertIn(b"https://panel.example.com:30228/health", data)

    def test_plain_ip_request_is_proxied_to_flask(self):
        async def upstream(reader, writer):
            data = await reader.read(4096)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nip!")
            await writer.drain()
            writer.close()

        async def run():
            up = await asyncio.start_server(upstream, "127.0.0.1", 0)
            up_port = up.sockets[0].getsockname()[1]
            server = await asyncio.start_server(
                lambda r, w: tls_front.handle_client(
                    r,
                    w,
                    upstream_host="127.0.0.1",
                    upstream_port=1,
                    public_port=30228,
                    default_host="127.0.0.1",
                    timeout=2,
                    plain_port=up_port,
                ),
                "127.0.0.1",
                0,
            )
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /health HTTP/1.1\r\nHost: 203.0.113.5:30228\r\n\r\n")
            await writer.drain()
            data = await reader.read()
            writer.close()
            server.close()
            up.close()
            await server.wait_closed()
            await up.wait_closed()
            return data

        self.assertIn(b"ip!", asyncio.run(run()))

    def test_tls_handshake_is_relayed_to_upstream(self):
        async def upstream(reader, writer):
            initial = await reader.read(10)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.write_eof()
            writer.close()

        async def run():
            up = await asyncio.start_server(upstream, "127.0.0.1", 0)
            up_port = up.sockets[0].getsockname()[1]
            server = await asyncio.start_server(
                lambda r, w: tls_front.handle_client(
                    r,
                    w,
                    upstream_host="127.0.0.1",
                    upstream_port=up_port,
                    public_port=30228,
                    default_host="",
                    timeout=2,
                ),
                "127.0.0.1",
                0,
            )
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"\x16\x03\x01fakehello")
            await writer.drain()
            data = await reader.read()
            writer.close()
            server.close()
            up.close()
            await server.wait_closed()
            await up.wait_closed()
            return data

        self.assertIn(b"ok", asyncio.run(run()))


if __name__ == "__main__":
    unittest.main()
