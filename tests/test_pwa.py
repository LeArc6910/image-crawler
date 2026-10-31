"""End-to-end: the PWA in headless Chromium reads an archive the crawler wrote to an S3 server."""

import functools
import io
import json
import os
import random
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import boto3
import pytest
import requests
from PIL import Image, ImageDraw

from image_crawler.archive import Archive
from image_crawler.crypto import Vault
from image_crawler.storage.s3 import S3Storage
from image_crawler.viewer import make_bundle, seal_bundle

NODE = shutil.which("node")
PWA = Path(__file__).parent.parent / "pwa"
SCRIPT = Path(__file__).parent / "js" / "pwa_e2e.mjs"
PASS = "correct horse battery"


def playwright_available() -> bool:
    if NODE is None:
        return False
    probe = "require('module').createRequire(process.cwd() + '/x')('playwright')"
    return subprocess.run([NODE, "-e", probe], capture_output=True).returncode == 0


def picture(seed: int, size=(640, 480)) -> bytes:
    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    top, bottom = [tuple(rnd.randrange(40, 230) for _ in range(3)) for _ in range(2)]
    for y in range(size[1]):
        t = y / size[1]
        draw.line([(0, y), (size[0], y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    for _ in range(4):
        x, y, r = rnd.randrange(size[0]), rnd.randrange(size[1]), rnd.randrange(30, 140)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=tuple(rnd.randrange(256) for _ in range(3)))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


@pytest.fixture
def r2_like(moto_server):
    """moto behind a shim that answers CORS preflights from the bucket's CORS rules, as R2 does.

    (moto itself answers OPTIONS on an existing key with 403, unlike S3 and R2.)
    """
    client = boto3.client("s3", endpoint_url=moto_server, region_name="us-east-1")
    no_proxy = {"http": None, "https": None}

    class Shim(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_OPTIONS(self):
            bucket = self.path.lstrip("/").split("/")[0].split("?")[0]
            origin = self.headers.get("Origin", "")
            method = self.headers.get("Access-Control-Request-Method", "")
            wanted = {h.strip().lower() for h in self.headers.get("Access-Control-Request-Headers", "").split(",") if h.strip()}
            rules = client.get_bucket_cors(Bucket=bucket)["CORSRules"]
            for rule in rules:
                allowed_headers = {h.lower() for h in rule.get("AllowedHeaders", [])}
                if origin in rule["AllowedOrigins"] and method in rule["AllowedMethods"] and wanted <= allowed_headers:
                    self.send_response(200)
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Access-Control-Allow-Methods", ", ".join(rule["AllowedMethods"]))
                    self.send_header("Access-Control-Allow-Headers", ", ".join(sorted(wanted)))
                    self.end_headers()
                    return
            self.send_response(403)
            self.end_headers()

        def do_GET(self):
            headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
            upstream = requests.get(moto_server + self.path, headers=headers, proxies=no_proxy)
            self.send_response(upstream.status_code)
            for k, v in upstream.headers.items():
                if k.lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding") \
                        and not k.lower().startswith("access-control-"):
                    self.send_header(k, v)
            if "Origin" in self.headers:
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Content-Length", str(len(upstream.content)))
            self.end_headers()
            self.wfile.write(upstream.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Shim)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield moto_server, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def static_server():
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(PWA))
    handler.log_message = lambda *a: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


@pytest.mark.skipif(not playwright_available(), reason="node + playwright not available")
def test_pwa_end_to_end(r2_like, static_server, tmp_path, key_file):
    moto_server, r2_endpoint = r2_like
    client = boto3.client("s3", endpoint_url=moto_server, region_name="us-east-1")
    client.create_bucket(Bucket="vault")
    storage = S3Storage("vault", client=client)
    storage.set_cors([static_server.rstrip("/")])  # what `image-crawler cors --origin ...` does for R2
    archive = Archive(Vault.from_key_file(key_file), storage, flush_every=4)
    sites = ["https://photos.example.com/", "https://gallery.example.org/"]
    for i in range(9):
        site = sites[i % 2]
        archive.store(picture(i, (640, 480) if i % 3 else (480, 640)), {
            "source_url": f"{site}img/photo-{i}.jpg", "page_url": site, "site": site,
            "filename": f"photo-{i}.jpg", "content_type": "image/jpeg",
            "fetched_at": f"2026-10-{10 + i:02d}T09:00:00+00:00",
        })
    archive.store(b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>', {
        "site": sites[0], "filename": "logo.svg", "content_type": "image/svg+xml",
        "fetched_at": "2026-10-01T09:00:00+00:00",
    })
    archive.flush()

    vault = Vault.from_key_file(key_file)
    text = seal_bundle(make_bundle(endpoint=r2_endpoint, region="us-east-1", bucket="vault", prefix="",
                                   access_key_id="test", secret_access_key="test",
                                   encryption_key=vault.encryption_key), PASS, iterations=1000)
    shots = Path(os.environ.get("PWA_SCREENSHOTS", tmp_path))
    shots.mkdir(parents=True, exist_ok=True)
    run = subprocess.run([NODE, str(SCRIPT), static_server, text, PASS, str(shots)],
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    r = json.loads(run.stdout)

    assert r["firstScreen"] == "setup"
    assert "비밀번호" in r["wrongPassError"]
    assert r["tiles"] == 10 and r["thumbsLoaded"] == 9 and r["placeholders"] == ["SVG"]
    assert r["count"] == "10장"
    assert r["sites"] == ["전체 사이트 (10)", "gallery.example.org (4)", "photos.example.com (6)"]
    assert r["filteredCount"] == "6장"
    assert r["viewerTitle"] == "photo-8.jpg"  # newest first
    assert r["viewer"] == {"w": 640, "h": 480}  # photo-8 is landscape (only multiples of 3 are portrait)
    assert "640×480" in r["viewerMeta"] and "photos.example.com" in r["viewerMeta"]
    assert r["afterLock"] == {"lockVisible": True, "tiles": 0}
    assert r["afterReload"] == "lock" and r["reunlockedTiles"] == 10
    assert r["storedBundleIsSealed"] is True
    assert r["serviceWorker"] is True
    assert r["errors"] == []
