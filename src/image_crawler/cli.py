from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from image_crawler import layout
from image_crawler.archive import Archive
from image_crawler.config import load_config
from image_crawler.crawler import Crawler, CrawlStats
from image_crawler.crypto import KIND_IMAGE, DecryptionError, Vault, generate_key_file
from image_crawler.quota import GB, FREE_TIER, GuardedStorage, QuotaExceeded, Usage, measure, soft_limits
from image_crawler.sites import add_sites, read_sites, remove_sites
from image_crawler.state import State
from image_crawler.storage import Storage, build_storage
from image_crawler.storage.s3 import S3Storage

EXIT_QUOTA = 3

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


def _quota_applies(cfg, storage: Storage) -> bool:
    return cfg.quota.enabled and isinstance(storage, S3Storage)


def _open_storage(cfg, state: State) -> Storage:
    """The configured backend, wrapped in the R2 quota guard when it applies."""
    storage = build_storage(cfg.storage)
    if not _quota_applies(cfg, storage):
        return storage
    report = measure(cfg.quota, state, storage.describe())
    _warn_if_near(cfg, report.effective)
    return GuardedStorage(storage, cfg.quota, state, report.effective, report.period_start.isoformat())


def _percentages(cfg, usage: Usage) -> dict[str, float]:
    limits = soft_limits(cfg.quota)
    return {
        "storage": 100 * usage.storage_bytes / limits.storage_bytes,
        "Class A": 100 * usage.class_a / limits.class_a,
        "Class B": 100 * usage.class_b / limits.class_b,
    }


def _warn_if_near(cfg, usage: Usage) -> None:
    for name, pct in _percentages(cfg, usage).items():
        if pct >= cfg.quota.warn_percent:
            print(f"warning: R2 {name} at {pct:.0f}% of the configured limit", file=sys.stderr)


def cmd_crawl(args, cfg) -> int:
    sites = args.site or read_sites(cfg.sites_file)
    if not sites:
        print(f"no sites to crawl; add some with `image-crawler add URL` ({cfg.sites_file})")
        return 1
    if args.max_depth is not None:
        cfg.crawl.max_depth = args.max_depth
    if args.max_pages is not None:
        cfg.crawl.max_pages_per_site = args.max_pages

    state = State(cfg.state_db)
    try:
        archive = Archive(Vault.from_key_file(cfg.key_file), _open_storage(cfg, state))
        storage = archive.storage
        if isinstance(storage, GuardedStorage):
            over = [n for n, pct in _percentages(cfg, storage.used).items() if pct >= 100]
            if over:
                print(f"R2 quota already reached ({', '.join(over)}); not crawling. See `image-crawler usage`.")
                return EXIT_QUOTA
        crawler = Crawler(cfg.crawl, archive, state)
        log.info("storing to %s", storage.describe())

        total = CrawlStats()
        for site in sites:
            log.info("crawling %s", site)
            stats = crawler.crawl_site(site)
            print(f"{site}: {_format_stats(stats)}")
            total += stats
            if crawler.stop_reason:
                print(f"stopped early: {crawler.stop_reason}")
                break
    finally:
        state.close()
    if len(sites) > 1:
        print(f"total: {_format_stats(total)}")
    return EXIT_QUOTA if crawler.stop_reason else 0


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


def _blobs_in_dir(root: Path):
    """(name, loader) for every image blob under a folder, e.g. one downloaded from the provider."""
    for path in sorted(root.rglob("*.bin")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith((layout.THUMBS, layout.INDEX)):
            continue
        name = path.stem
        if len(name) == 64 and all(c in "0123456789abcdef" for c in name):
            yield name, path.read_bytes


def cmd_restore(args, cfg) -> int:
    """Download (or read) encrypted blobs, decrypt them and write the original images."""
    vault = Vault.from_key_file(cfg.key_file)
    out_dir = Path(args.out).expanduser()

    if args.from_dir:
        items = _blobs_in_dir(Path(args.from_dir).expanduser())
    else:
        archive = Archive(vault, _open_storage(cfg, State(cfg.state_db)))
        print(f"restoring from {archive.storage.describe()}")
        items = (
            (n, (lambda n=n: archive.storage.get(layout.image_key(n))))
            for n in archive.image_names()
        )

    restored = failed = existing = 0
    for name, load in items:
        try:
            opened = vault.open(load(), kind=KIND_IMAGE, name=name)
        except QuotaExceeded as e:
            print(f"stopped early: {e}")
            print(f"restored {restored}, already present {existing}, failed {failed} -> {out_dir}")
            return EXIT_QUOTA
        except DecryptionError as e:
            log.warning("cannot decrypt %s: %s", name, e)
            failed += 1
            continue
        meta = opened.metadata
        if args.site and args.site not in meta.get("site", ""):
            continue
        site_dir = _safe_part(urlsplit(meta.get("site", "")).netloc, "unknown-site")
        filename = f"{name[:12]}_{_safe_part(meta.get('filename', ''), 'image')}"
        target = out_dir / site_dir / filename
        if target.exists():
            existing += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(opened.content)
        restored += 1
    print(f"restored {restored}, already present {existing}, failed {failed} -> {out_dir}")
    return 1 if failed else 0


def cmd_reindex(args, cfg) -> int:
    """Add thumbnails and index entries for stored images the index doesn't know about yet."""
    archive = Archive(Vault.from_key_file(cfg.key_file), _open_storage(cfg, State(cfg.state_db)))
    print(f"checking {archive.storage.describe()}")
    indexed = {entry["object"] for entry in archive.read_index()}
    missing = [n for n in archive.image_names() if n not in indexed]
    print(f"{len(indexed)} indexed, {len(missing)} missing")
    failed = 0
    for i, name in enumerate(missing, 1):
        try:
            opened = archive.open_image(name)
            archive.add_to_index(name, opened.content, opened.metadata)
        except QuotaExceeded as e:
            print(f"stopped early: {e}")
            archive.flush()
            return EXIT_QUOTA
        except Exception as e:
            log.warning("cannot index %s: %s", name, e)
            failed += 1
        if i % 50 == 0:
            print(f"  {i}/{len(missing)}")
    archive.flush()
    print(f"indexed {len(missing) - failed}, failed {failed}")
    return 1 if failed else 0


def cmd_cors(args, cfg) -> int:
    storage = build_storage(cfg.storage)
    if not isinstance(storage, S3Storage):
        print("cors only applies to the s3 backend (e.g. Cloudflare R2)", file=sys.stderr)
        return 2
    storage.set_cors(args.origin)
    print(f"CORS on {storage.describe()} now allows GET/HEAD from: {', '.join(args.origin)}")
    return 0


def cmd_usage(args, cfg) -> int:
    """Show this billing period's R2 usage against the configured limits and the free tier."""
    storage = build_storage(cfg.storage)
    if not _quota_applies(cfg, storage):
        print("quota tracking applies to the s3 backend (Cloudflare R2) with [quota] enabled")
        return 0
    state = State(cfg.state_db)
    try:
        report = measure(cfg.quota, state, storage.describe())
    finally:
        state.close()

    limits, usage, cloud = soft_limits(cfg.quota), report.effective, report.cloudflare
    print(f"R2 usage {report.period_start} ~ {report.period_end} (UTC), {storage.describe()}")
    print(f"  {'':8}{'this machine':>14}{'cloudflare':>14}{'limit':>14}{'used':>7}{'free tier':>14}")
    for label, attr, fmt in (
        ("storage", "storage_bytes", lambda v: f"{v / GB:.2f} GB"),
        ("Class A", "class_a", "{:,}".format),
        ("Class B", "class_b", "{:,}".format),
    ):
        limit = getattr(limits, attr)
        print(
            f"  {label:8}{fmt(getattr(report.local, attr)):>14}"
            f"{fmt(getattr(cloud, attr)) if cloud else '-':>14}{fmt(limit):>14}"
            f"{100 * getattr(usage, attr) / limit:>6.0f}%{fmt(getattr(FREE_TIER, attr)):>14}"
        )
    if report.cloudflare is None:
        reason = report.cloudflare_error or "set quota.cloudflare_account_id and CLOUDFLARE_API_TOKEN"
        print(f"  cloudflare: not used ({reason}); counts cover this machine only")

    near = [n for n, pct in _percentages(cfg, usage).items() if pct >= cfg.quota.warn_percent]
    if near:
        print(f"WARNING: {', '.join(near)} at or above {cfg.quota.warn_percent}% of the limit")
    return 1 if (args.check and near) else 0


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

    p = sub.add_parser("reindex", help="add thumbnails/index entries for images missing from the index")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("cors", help="allow the PWA viewer's origin to read the bucket (s3/R2 only)")
    p.add_argument("--origin", action="append", required=True,
                   help="viewer origin, e.g. https://you.github.io (repeatable)")
    p.set_defaults(func=cmd_cors)

    p = sub.add_parser("usage", help="show this month's R2 usage against the quota")
    p.add_argument("--check", action="store_true",
                   help="exit with status 1 when any metric is at or above quota.warn_percent (for cron)")
    p.set_defaults(func=cmd_usage)
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
