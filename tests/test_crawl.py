import pytest

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


def album_site(site):
    """A list → album site: list pages chain with ?page links, album pages show their own photos
    next to covers of other albums, and there are pages a rule should not follow."""
    photos = {f"/img/a{a}-{n}.png": make_png(1000 + a * 10 + n) for a in (1, 2, 3) for n in (1, 2)}
    covers = {f"/img/cover{a}.png": make_png(900 + a) for a in (1, 2, 3)}
    for path, data in {**photos, **covers}.items():
        site.image(path, data)
    site.page("/list/1", '<a href="/album/1"><img src="/img/cover1.png"></a>'
                         '<a href="/album/2"><img src="/img/cover2.png"></a><a href="/list/2">next</a>'
                         '<a href="/download/1">download</a><a href="/user/9">user</a>')
    site.page("/list/2", '<a href="/album/3"><img src="/img/cover3.png"></a><a href="/list/1">prev</a>')
    for a in (1, 2, 3):
        other = 3 if a != 3 else 1
        site.page(f"/album/{a}", f'<h1> Album  number {a} </h1>'
                                 f'<img class="photo" src="/img/a{a}-1.png"><img class="photo" src="/img/a{a}-2.png">'
                                 f'<a href="/album/{other}"><img src="/img/cover{other}.png"></a>')
    site.page("/download/1", '<img src="/img/never.png">')
    site.page("/user/9", '<img src="/img/never.png">')
    return photos


def album_rule(**overrides):
    from image_crawler.config import SiteRule

    base = dict(host="127.0.0.1", follow=[r"^/list/\d+$", r"^/album/\d+$"], album_pages=r"^/album/\d+$",
                album_images="img.photo", other_images=False, max_depth=50)
    base.update(overrides)
    return SiteRule(**base)


def test_album_rule_stores_only_album_photos_with_their_folder(site, tmp_path, key_file):
    photos = album_site(site)
    crawler, archive = make_crawler(tmp_path, key_file, max_depth=1, rules=[album_rule()])
    stats = crawler.crawl_site(site.url + "/list/1")

    assert stats.stored == 6 and stats.errors == 0 and stats.albums_finished == 3
    assert "/download/1" not in site.hits and "/user/9" not in site.hits
    assert not any("cover" in h for h in site.hits)  # covers are neither on album photos nor stored
    archive.flush()
    entries = archive.read_index()
    folders = {(e["album"], e["position"], e["source_url"].rsplit("/", 1)[1]) for e in entries}
    assert folders == {(f"Album number {a}", n, f"a{a}-{n}.png") for a in (1, 2, 3) for n in (1, 2)}
    assert {archive.open_image(e["object"]).content for e in entries} == set(photos.values())

    # The next run fetches the list pages again (new albums appear there) but not finished albums.
    site.hits.clear()
    again = crawler.crawl_site(site.url + "/list/1")
    assert again.albums_done == 3 and again.pages == 2 and again.stored == 0
    assert not any(h.startswith("/album/") for h in site.hits)


def test_album_with_a_failed_photo_is_retried(site, tmp_path, key_file):
    album_site(site)
    del site.routes["/img/a2-2.png"]  # 404
    crawler, _ = make_crawler(tmp_path, key_file, rules=[album_rule()])
    stats = crawler.crawl_site(site.url + "/list/1")
    assert stats.errors == 1 and stats.albums_finished == 2
    site.image("/img/a2-2.png", make_png(4321))
    site.hits.clear()
    again = crawler.crawl_site(site.url + "/list/1")
    assert again.stored == 1 and again.albums_finished == 1 and again.albums_done == 2
    assert "/album/2" in site.hits


def test_without_rules_images_get_the_page_title_as_folder(site, tmp_path, key_file):
    site.page("/", "<title>Home page</title><img src='/a.png'>")
    site.image("/a.png", make_png(2000))
    crawler, archive = make_crawler(tmp_path, key_file)
    crawler.crawl_site(site.url + "/")
    archive.flush()
    [entry] = archive.read_index()
    assert entry["album"] == "Home page" and entry["position"] == 1


def test_rules_from_config_and_restore_into_album_folders(site, tmp_path, key_file, capsys):
    album_site(site)
    config = tmp_path / "config.toml"
    config.write_text(f'''
key_file = "{key_file}"
state_db = "state.db"
[crawl]
delay_seconds = 0
min_image_bytes = 100
[[crawl.rules]]
host = "127.0.0.1"
follow = ['^/list/\\d+$', '^/album/\\d+$']
album_pages = '^/album/\\d+$'
album_images = "img.photo"
other_images = false
max_depth = 50
[storage.local]
path = "vault"
''')
    assert main(["-c", str(config), "crawl", "--site", site.url + "/list/1"]) == 0
    assert "albums: 3 finished, 0 done earlier" in capsys.readouterr().out
    assert main(["-c", str(config), "restore", "--out", str(tmp_path / "out")]) == 0
    files = sorted(p.relative_to(tmp_path / "out").parts[1:] for p in (tmp_path / "out").rglob("*.png"))
    assert [(folder, name[:4]) for folder, name in files] == [
        (f"Album_number_{a}", f"{n:03d}_") for a in (1, 2, 3) for n in (1, 2)
    ]


def test_bad_rule_is_rejected(tmp_path):
    from image_crawler.config import load_config

    config = tmp_path / "config.toml"
    config.write_text('[[crawl.rules]]\nhost = "x.com"\nalbum_pages = "("\n')
    with pytest.raises(ValueError, match="bad pattern"):
        load_config(config)
    config.write_text('[[crawl.rules]]\nhost = "x.com"\nalbum_images = "img[[["\n')
    with pytest.raises(ValueError, match="bad CSS selector"):
        load_config(config)


def test_album_list_in_page_data_gets_photos_behind_more_button(site, tmp_path, key_file, capsys):
    # The page shows 2 photos; its data (escaped JSON, as React server payloads are) lists all 4.
    photos = {f"/img/{c}.png": make_png(1500 + i) for i, c in enumerate("abcd")}
    for path, data in photos.items():
        site.image(path, data)
    site.page("/list/1", '<a href="/album/1">x</a>')
    payload = '\\"images\\":[\\"a\\",\\"b\\",\\"c\\",\\"d\\"],\\"title\\":\\"x\\"'
    site.page("/album/1", f'<h1>Full</h1><img class="photo" src="/img/a.png"><img class="photo" src="/img/b.png">'
                          f'<a class="more">More</a><script>self.__next_f.push([1,"{payload}"])</script>')
    shown_only = album_rule()
    crawler, archive = make_crawler(tmp_path, key_file, rules=[shown_only])
    assert crawler.crawl_site(site.url + "/list/1").stored == 2

    # With the list rule, --redo-albums reopens the finished album and adds what was hidden.
    crawler.settings.rules = [album_rule(album_list=r'\\"images\\":\[(.*?)\]', album_item=r'\\"(\w+)\\"',
                                         album_url=site.url + "/img/{}.png")]
    assert crawler.crawl_site(site.url + "/list/1").albums_done == 1
    crawler.redo_albums = True
    again = crawler.crawl_site(site.url + "/list/1")
    assert again.stored == 2 and again.already_seen == 2
    archive.flush()
    entries = sorted(archive.read_index(), key=lambda e: e["position"])
    assert [(e["position"], e["source_url"].rsplit("/", 1)[1]) for e in entries] == [
        (1, "a.png"), (2, "b.png"), (3, "c.png"), (4, "d.png")
    ]
    assert {e["album"] for e in entries} == {"Full"}


def test_album_list_missing_falls_back_to_shown_images(site, tmp_path, key_file):
    album_site(site)
    rule = album_rule(album_list=r'"images":\[(.*?)\]', album_item=r'"(\w+)"')
    crawler, _ = make_crawler(tmp_path, key_file, rules=[rule])
    assert crawler.crawl_site(site.url + "/list/1").stored == 6


def test_cover_stored_from_a_list_page_moves_into_its_album(site, tmp_path, key_file):
    album_site(site)
    site.page("/list/1", '<title>List</title><img src="/img/a1-1.png"><a href="/album/1">x</a>')
    crawler, archive = make_crawler(tmp_path, key_file, max_depth=0)
    assert crawler.crawl_site(site.url + "/list/1").stored == 1  # stored as a list-page image

    crawler.settings.rules = [album_rule()]
    stats = crawler.crawl_site(site.url + "/list/1")
    assert stats.regrouped == 1 and stats.stored >= 1
    archive.flush()
    album1 = [e for e in archive.read_index() if e["page_url"].endswith("/album/1")]
    assert sorted((e["position"], e["album"]) for e in album1) == [(1, "Album number 1"), (2, "Album number 1")]
    assert not [e for e in archive.read_index() if e["page_url"].endswith("/list/1")]
    # Running again finds nothing more to move.
    crawler.redo_albums = True
    assert crawler.crawl_site(site.url + "/list/1").regrouped == 0
