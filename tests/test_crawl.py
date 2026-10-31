from image_crawler.archive import Archive
from image_crawler.cli import main
from image_crawler.config import CrawlSettings
from image_crawler.crawler import Crawler
from image_crawler.crypto import Vault
from image_crawler.state import State
from image_crawler.storage.local import LocalStorage

from conftest import make_png


def settings(**overrides):
    base = dict(delay_seconds=0, min_image_bytes=100, max_image_bytes=50_000)
    base.update(overrides)
    return CrawlSettings(**base)


def make_crawler(tmp_path, key_file, **overrides):
    archive = Archive(Vault.from_key_file(key_file), LocalStorage(tmp_path / "vault"))
    state = State(tmp_path / "state.db")
    return Crawler(settings(**overrides), archive, state), archive


def test_crawls_follows_links_and_stores_encrypted(site, tmp_path, key_file):
    cat, dog = make_png(2000), make_png(3000)
    site.page("/", '<img src="/cat.png"><a href="/more">more</a><a href="https://other.example/">x</a>')
    site.page("/more", '<img src="/dog.png"><img src="/cat.png"><a href="/deeper">deeper</a>')
    site.page("/deeper", '<img src="/never.png">')
    site.image("/cat.png", cat)
    site.image("/dog.png", dog)

    crawler, archive = make_crawler(tmp_path, key_file, max_depth=1)
    stats = crawler.crawl_site(site.url + "/")

    assert stats.pages == 2
    assert stats.stored == 2
    assert stats.already_seen == 1  # cat.png linked from both pages
    assert "/deeper" not in site.hits
    names = set(archive.image_names())
    assert len(names) == 2
    restored = {archive.open_image(n).content for n in names}
    assert restored == {cat, dog}
    for path in (tmp_path / "vault").rglob("*.bin"):
        assert b"PNG" not in path.read_bytes()[:64]


def test_second_run_skips_known_images_and_dedupes_by_content(site, tmp_path, key_file):
    img = make_png(2000)
    site.page("/", '<img src="/a.png">')
    site.image("/a.png", img)
    crawler, archive = make_crawler(tmp_path, key_file)
    assert crawler.crawl_site(site.url + "/").stored == 1

    site.page("/", '<img src="/a.png"><img src="/copy-of-a.png">')
    site.image("/copy-of-a.png", img)
    stats = crawler.crawl_site(site.url + "/")
    assert stats.stored == 0
    assert stats.already_seen == 1
    assert stats.duplicates == 1
    assert len(list(archive.image_names())) == 1


def test_size_limits_and_non_images_are_skipped(site, tmp_path, key_file):
    site.page("/", '<img src="/tiny.png"><img src="/huge.png"><img src="/fake.png"><img src="/ok.png">')
    site.image("/tiny.png", make_png(50))
    site.image("/huge.png", make_png(60_000))
    site.image("/fake.png", b"<html>" + b"x" * 500, ctype="text/html")
    site.image("/ok.png", make_png(1000))
    crawler, _ = make_crawler(tmp_path, key_file)
    stats = crawler.crawl_site(site.url + "/")
    assert stats.stored == 1
    assert stats.skipped == 3


def test_robots_txt_is_respected(site, tmp_path, key_file):
    site.routes["/robots.txt"] = (200, "text/plain", b"User-agent: *\nDisallow: /private\n")
    site.page("/", '<img src="/private/a.png"><img src="/b.png"><a href="/private/page">p</a>')
    site.page("/private/page", '<img src="/c.png">')
    site.image("/private/a.png", make_png(1000))
    site.image("/b.png", make_png(1000))
    site.image("/c.png", make_png(1000))
    crawler, _ = make_crawler(tmp_path, key_file)
    stats = crawler.crawl_site(site.url + "/")
    assert stats.stored == 1
    assert "/private/a.png" not in site.hits
    assert "/private/page" not in site.hits


def test_cli_end_to_end(site, tmp_path, key_file, capsys):
    img = make_png(4000)
    site.page("/", '<img src="/photos/sunset.png?v=2">')
    site.image("/photos/sunset.png?v=2", img)
    config = tmp_path / "config.toml"
    config.write_text(
        f'key_file = "{key_file}"\n'
        'state_db = "state.db"\n'
        "[crawl]\ndelay_seconds = 0\nmin_image_bytes = 100\n"
        '[storage]\nbackend = "local"\n[storage.local]\npath = "vault"\n'
    )

    assert main(["-c", str(config), "add", site.url + "/", site.url + "/"]) == 0
    assert main(["-c", str(config), "sites"]) == 0
    assert capsys.readouterr().out.count(site.url) == 2  # "added" line + listing
    assert main(["-c", str(config), "crawl"]) == 0
    assert "1 stored" in capsys.readouterr().out

    out = tmp_path / "restored"
    assert main(["-c", str(config), "restore", "--out", str(out)]) == 0
    files = list(out.rglob("*.png"))
    assert len(files) == 1
    assert files[0].name.endswith("_sunset.png")
    assert files[0].read_bytes() == img

    out2 = tmp_path / "restored2"
    assert main(["-c", str(config), "restore", "--out", str(out2), "--from-dir", str(tmp_path / "vault")]) == 0
    assert [p.read_bytes() for p in out2.rglob("*.png")] == [img]

    assert main(["-c", str(config), "remove", site.url + "/"]) == 0
    assert main(["-c", str(config), "crawl"]) == 1


def test_rate_limited_responses_are_retried(site, tmp_path, key_file, monkeypatch):
    monkeypatch.setattr("image_crawler.crawler.time.sleep", lambda s: None)
    img = make_png(1000)
    site.page("/", '<img src="/busy.png">')
    site.routes["/busy.png"] = (429, "text/plain", b"slow down")
    crawler, _ = make_crawler(tmp_path, key_file)
    real_get = crawler.fetcher.session.get

    def get(url, **kw):
        if "/busy.png" in site.hits:  # recover after the first 429
            site.image("/busy.png", img)
        return real_get(url, **kw)

    monkeypatch.setattr(crawler.fetcher.session, "get", get)
    assert crawler.crawl_site(site.url + "/").stored == 1
    assert site.hits.count("/busy.png") == 2


def test_crawl_auto_compacts_the_index(site, tmp_path, key_file, capsys, monkeypatch):
    monkeypatch.setattr("image_crawler.cli.AUTO_COMPACT_SEGMENTS", 1)
    site.page("/a", '<img src="/1.png">')
    site.page("/b", '<img src="/2.png">')
    site.image("/1.png", make_png(500))
    site.image("/2.png", make_png(500))
    config = tmp_path / "config.toml"
    config.write_text(
        f'key_file = "{key_file}"\nstate_db = "state.db"\n[crawl]\ndelay_seconds = 0\nmin_image_bytes = 100\n'
        '[storage.local]\npath = "vault"\n'
    )
    assert main(["-c", str(config), "crawl", "--site", site.url + "/a", "--site", site.url + "/b"]) == 0
    assert "compacted index: 2 segments -> 1" in capsys.readouterr().out
    assert len(list((tmp_path / "vault" / "index").iterdir())) == 1
