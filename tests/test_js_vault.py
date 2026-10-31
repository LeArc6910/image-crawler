"""Cross-language check: the PWA's vault.js must open what the Python crawler writes."""

import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from image_crawler.archive import Archive
from image_crawler.crypto import Vault, load_key_file
from image_crawler.storage.local import LocalStorage

from conftest import make_photo

NODE = shutil.which("node")
SCRIPT = Path(__file__).parent / "js" / "check_vault.mjs"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_vault_js_opens_python_archive(tmp_path, key_file):
    archive = Archive(Vault.from_key_file(key_file), LocalStorage(tmp_path / "vault"), flush_every=2)
    photos = [make_photo(300, 200), make_photo(50, 80, "PNG"), b'<svg xmlns="http://www.w3.org/2000/svg"/>']
    for i, photo in enumerate(photos):
        archive.store(photo, {"source_url": f"https://example.com/한글-{i}", "site": "https://example.com/"})
    archive.flush()
    # A later entry for the same object (an image moved into its album) replaces the first one.
    first = archive.read_index()[0]["object"]
    archive.add_to_index(first, photos[0], {"source_url": "https://example.com/한글-0", "album": "앨범", "position": 1})
    archive.flush()

    master = base64.b64encode(load_key_file(key_file)).decode()
    result = subprocess.run(
        [NODE, str(SCRIPT), str(tmp_path / "vault"), master], capture_output=True, text=True, check=True
    )
    out = json.loads(result.stdout)

    assert out["entries"] == archive.read_index()
    assert len(out["entries"]) == 3 and out["entries"][0]["album"] == "앨범"
    assert [i["sha256"] for i in out["images"]] == [hashlib.sha256(p).hexdigest() for p in photos]
    assert [i["source_url"] for i in out["images"]] == [f"https://example.com/한글-{i}" for i in range(3)]
    assert out["images"][0]["thumbBytes"] > 0 and out["images"][2]["thumbBytes"] is None
    assert out["swapRejected"] is True
