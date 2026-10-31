from __future__ import annotations

import io
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def make_png(size: int) -> bytes:
    """A PNG signature followed by random bytes: sniffed as PNG, unique per call."""
    return b"\x89PNG\r\n\x1a\n" + os.urandom(size - 8)


class FakeSite:
    """In-process HTTP server; tests fill `routes` with path -> (status, content type, body)."""

    def __init__(self):
        self.routes: dict[str, tuple[int, str, bytes]] = {}
        self.hits: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                site.hits.append(self.path)
                status, ctype, body = site.routes.get(self.path, (404, "text/plain", b"not found"))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def page(self, path: str, html: str) -> None:
        self.routes[path] = (200, "text/html; charset=utf-8", html.encode())

    def image(self, path: str, data: bytes, ctype: str = "image/png") -> None:
        self.routes[path] = (200, ctype, data)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site():
    s = FakeSite()
    yield s
    s.close()


@pytest.fixture
def key_file(tmp_path):
    from image_crawler.crypto import generate_key_file

    path = tmp_path / "key"
    generate_key_file(path)
    return path
