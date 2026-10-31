from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from image_crawler.config import load_config
from image_crawler.crawler import Crawler, CrawlStats
from image_crawler.crypto import DecryptionError, Vault, generate_key_file
from image_crawler.sites import add_sites, read_sites, remove_sites
from image_crawler.state import State
from image_crawler.storage import build_storage
from image_crawler.storage.base import name_from_path

log = logging.getLogger("image_crawler")


def cmd_keygen(args, cfg) -> int:
    path = Path(args.key_file).expanduser() if args.key_file else cfg.key_file
    generate_key_file(path, overwrite=args.force)
    print(f"created key: {path}")
    print("Back this file up somewhere safe and offline. Without it the stored images cannot be decrypted.")
    return 0


def cmd_add(args, cfg) -> int:
    added = add_sites(cfg.sites_file, args.urls)
    for url in added:
        print(f"added {url}")
    if not added:
        print("nothing new to add")
    return 0


def cmd_remove(args, cfg) -> int:
    removed = remove_sites(cfg.sites_file, args.urls)
    for url in removed:
        print(f"removed {url}")
    if not removed:
        print("no matching sites")
    return 0


def cmd_sites(args, cfg) -> int:
    sites = read_sites(cfg.sites_file)
    if not sites:
        print(f"no sites registered in {cfg.sites_file}")
    for url in sites:
        print(url)
    return 0


def cmd_crawl(args, cfg) -> int:
    sites = args.site or read_sites(cfg.sites_file)
    if not sites:
        print(f"no sites to crawl; add some with `image-crawler add URL` ({cfg.sites_file})")
        return 1
    if args.max_depth is not None:
        cfg.crawl.max_depth = args.max_depth
    if args.max_pages is not None:
        cfg.crawl.max_pages_per_site = args.max_pages

    vault = Vault.from_key_file(cfg.key_file)
    storage = build_storage(cfg.storage)
    state = State(cfg.state_db)
    crawler = Crawler(cfg.crawl, vault, storage, state)
    log.info("storing to %s", storage.describe())

    total = CrawlStats()
    try:
        for site in sites:
            log.info("crawling %s", site)
            stats = crawler.crawl_site(site)
            print(f"{site}: {_format_stats(stats)}")
            total += stats
    finally:
        state.close()
    if len(sites) > 1:
        print(f"total: {_format_stats(total)}")
    return 0


def _format_stats(s: CrawlStats) -> str:
    return (
        f"{s.pages} page(s), {s.images_found} image(s) found, {s.stored} stored, "
        f"{s.duplicates} duplicate(s), {s.already_seen} already seen, "
        f"{s.skipped} skipped, {s.errors} error(s)"
    )


_UNSAFE = re.compile(r"[^\w.\-]+")


def _safe_part(value: str, fallback: str) -> str:
    value = _UNSAFE.sub("_", value).strip("._")
    return value[:120] or fallback


def cmd_restore(args, cfg) -> int:
    """Download (or read) encrypted blobs, decrypt them and write the original images."""
    vault = Vault.from_key_file(cfg.key_file)
    out_dir = Path(args.out).expanduser()

    if args.from_dir:
        files = [p for p in Path(args.from_dir).expanduser().rglob("*.bin") if p.is_file()]
        items = ((name_from_path(p.name), p.read_bytes) for p in files)
    else:
        storage = build_storage(cfg.storage)
        print(f"restoring from {storage.describe()}")
        items = ((n, (lambda n=n: storage.get(n))) for n in storage.list_names())

    restored = failed = existing = 0
    for name, load in items:
        try:
            opened = vault.open(load())
        except DecryptionError as e:
            log.warning("cannot decrypt %s: %s", name, e)
            failed += 1
            continue
        meta = opened.metadata
        site_dir = _safe_part(urlsplit(meta.get("site", "")).netloc, "unknown-site")
        if args.site and args.site not in meta.get("site", ""):
            continue
        filename = f"{(name or 'x')[:12]}_{_safe_part(meta.get('filename', ''), 'image')}"
        target = out_dir / site_dir / filename
        if target.exists():
            existing += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(opened.content)
        restored += 1
    print(f"restored {restored}, already present {existing}, failed {failed} -> {out_dir}")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="image-crawler",
        description="Crawl images from registered sites, encrypt them locally and upload them.",
    )
    parser.add_argument("-c", "--config", help="path to config.toml (default: ./config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show per-page/per-image logs")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("keygen", help="create the encryption key file")
    p.add_argument("--key-file", help="where to write the key (default: key_file from config)")
    p.add_argument("--force", action="store_true", help="overwrite an existing key (old blobs become unreadable)")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser("add", help="register site URL(s) to crawl")
    p.add_argument("urls", nargs="+")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="unregister site URL(s)")
    p.add_argument("urls", nargs="+")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("sites", help="list registered sites")
    p.set_defaults(func=cmd_sites)

    p = sub.add_parser("crawl", help="crawl registered sites and upload new images")
    p.add_argument("--site", action="append", help="crawl only this URL (repeatable)")
    p.add_argument("--max-depth", type=int, help="override crawl.max_depth")
    p.add_argument("--max-pages", type=int, help="override crawl.max_pages_per_site")
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("restore", help="download and decrypt stored images")
    p.add_argument("--out", required=True, help="directory to write decrypted images to")
    p.add_argument("--from-dir", help="decrypt *.bin blobs from a local folder instead of the backend")
    p.add_argument("--site", help="only restore images whose site URL contains this text")
    p.set_defaults(func=cmd_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        cfg = load_config(args.config)
        return args.func(args, cfg)
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
