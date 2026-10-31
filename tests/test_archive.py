import io

from PIL import Image

from image_crawler import layout
from image_crawler.archive import Archive
from image_crawler.cli import main
from image_crawler.crypto import KIND_IMAGE, KIND_THUMB, Vault
from image_crawler.storage.local import LocalStorage
from image_crawler.thumbs import THUMB_SIZE, make_preview

from conftest import make_photo, make_png


def archive_for(tmp_path, key_file, **kw):
    return Archive(Vault.from_key_file(key_file), LocalStorage(tmp_path / "vault"), **kw)


def meta(url):
    return {"source_url": url, "site": "https://example.com/", "filename": url.rsplit("/", 1)[-1]}


def test_preview_downscales_and_keeps_original_dimensions():
    preview = make_preview(make_photo(1200, 600))
    assert (preview.width, preview.height) == (1200, 600)
    thumb = Image.open(io.BytesIO(preview.thumbnail))
    assert thumb.format == "WEBP"
    assert thumb.size == (THUMB_SIZE, THUMB_SIZE // 2)


def test_preview_handles_rotation_alpha_and_undecodable_data():
    exif = Image.Exif()
    exif[0x0112] = 6  # rotated 90°: stored 400x200, displayed 200x400
    rotated = make_preview(make_photo(400, 200, exif=exif))
    assert (rotated.width, rotated.height) == (200, 400)
    assert Image.open(io.BytesIO(rotated.thumbnail)).size == (160, 320)

    rgba = Image.new("RGBA", (50, 50), (255, 0, 0, 128))
    out = io.BytesIO()
    rgba.save(out, "PNG")
    assert Image.open(io.BytesIO(make_preview(out.getvalue()).thumbnail)).mode == "RGBA"

    assert make_preview(b"<svg xmlns='http://www.w3.org/2000/svg'/>").thumbnail is None


def test_store_writes_image_thumbnail_and_index(tmp_path, key_file):
    archive = archive_for(tmp_path, key_file)
    photo = make_photo(800, 600)
    name = archive.store(photo, meta("https://example.com/a.jpg"))
    archive.store(make_png(500), meta("https://example.com/b.png"))  # undecodable: no thumbnail
    assert archive.read_index() == []  # nothing flushed yet
    segment = archive.flush()
    assert archive.flush() is None

    keys = list(archive.storage.list())
    assert layout.index_key(segment) in keys
    entries = archive.read_index()
    assert [e["filename"] for e in entries] == ["a.jpg", "b.png"]
    first = entries[0]
    assert first["object"] == name
    assert (first["width"], first["height"], first["size"]) == (800, 600, len(photo))
    assert first["thumb"] == archive.vault.thumb_name(name)
    assert entries[1]["thumb"] is None

    thumb = archive.vault.open(
        archive.storage.get(layout.thumb_key(first["thumb"])), kind=KIND_THUMB, name=first["thumb"]
    )
    assert Image.open(io.BytesIO(thumb.content)).size == (THUMB_SIZE, 240)
    assert len(archive.storage.get(layout.thumb_key(first["thumb"]))) % 4096 == 32  # small padding
    assert archive.open_image(name).content == photo
    assert sorted(archive.image_names()) == sorted([name, entries[1]["object"]])


def test_index_flushes_in_segments_and_dedupes(tmp_path, key_file):
    archive = archive_for(tmp_path, key_file, flush_every=2)
    photos = [make_photo(40, 40) for _ in range(5)]
    names = [archive.store(p, meta(f"https://example.com/{i}.jpg")) for i, p in enumerate(photos)]
    archive.add_to_index(names[0], photos[0], meta("https://example.com/again.jpg"))
    archive.flush()
    assert len(list(archive.storage.list(layout.INDEX))) == 3
    entries = archive.read_index()
    assert [e["object"] for e in entries] == names
    assert entries[0]["filename"] == "again.jpg"  # the latest entry wins, in the first one's place


def test_reindex_backfills_images_missing_from_index(tmp_path, key_file, capsys):
    archive = archive_for(tmp_path, key_file)
    indexed = archive.store(make_photo(64, 64), meta("https://example.com/kept.jpg"))
    archive.flush()
    # Simulate an image uploaded without index entry (crash before flush, or a v1 upload).
    orphan = make_photo(64, 64)
    orphan_name = archive.vault.object_name(orphan)
    archive.storage.put(
        layout.image_key(orphan_name),
        archive.vault.seal(orphan, meta("https://example.com/orphan.jpg"), kind=KIND_IMAGE, name=orphan_name),
    )

    config = tmp_path / "config.toml"
    config.write_text(f'key_file = "{key_file}"\n[storage.local]\npath = "vault"\n')
    assert main(["-c", str(config), "reindex"]) == 0
    assert "1 indexed, 1 missing" in capsys.readouterr().out
    entries = archive.read_index()
    assert [e["object"] for e in entries] == [indexed, orphan_name]
    assert entries[1]["filename"] == "orphan.jpg" and entries[1]["thumb"]

    assert main(["-c", str(config), "reindex"]) == 0
    assert "2 indexed, 0 missing" in capsys.readouterr().out


def test_restore_ignores_thumbnails_and_index(tmp_path, key_file):
    archive = archive_for(tmp_path, key_file)
    photo = make_photo(64, 64)
    archive.store(photo, meta("https://example.com/p.jpg"))
    archive.flush()
    config = tmp_path / "config.toml"
    config.write_text(f'key_file = "{key_file}"\n[storage.local]\npath = "vault"\n')
    for extra in ([], ["--from-dir", str(tmp_path / "vault")]):
        out = tmp_path / f"out{len(extra)}"
        assert main(["-c", str(config), "restore", "--out", str(out), *extra]) == 0
        assert [p.read_bytes() for p in out.rglob("*.jpg")] == [photo]


def test_compact_merges_segments_and_keeps_entries(tmp_path, key_file, monkeypatch):
    archive = archive_for(tmp_path, key_file, flush_every=2)
    names = [archive.store(make_photo(32, 32), meta(f"https://example.com/{i}.jpg")) for i in range(7)]
    archive.flush()
    before = archive.read_index()
    assert len(archive.segment_keys()) == 4

    assert archive.compact() == (4, 1)
    assert len(archive.segment_keys()) == 1
    assert archive.read_index() == before
    assert [e["object"] for e in before] == names
    assert archive.compact() == (1, 1)  # nothing to do

    monkeypatch.setattr("image_crawler.archive.COMPACT_SEGMENT_SIZE", 3)
    archive.store(make_photo(32, 32), meta("https://example.com/late.jpg"))  # pending: flushed by compact
    assert archive.compact() == (2, 3)
    assert [e["object"] for e in archive.read_index()][:7] == names


def test_compact_command_and_restore_limit(tmp_path, key_file, capsys):
    archive = archive_for(tmp_path, key_file, flush_every=1)
    for i in range(3):
        archive.store(make_photo(32, 32), meta(f"https://example.com/{i}.jpg"))
    config = tmp_path / "config.toml"
    config.write_text(f'key_file = "{key_file}"\n[storage.local]\npath = "vault"\n')

    assert main(["-c", str(config), "compact"]) == 0
    assert "index segments: 3 -> 1" in capsys.readouterr().out

    out = tmp_path / "restored"
    assert main(["-c", str(config), "restore", "--out", str(out), "--limit", "2"]) == 0
    assert "stopping after --limit 2" in capsys.readouterr().out
    assert len(list(out.rglob("*.jpg"))) == 2
