from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import secrets
import sys
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from image_crawler import layout
from image_crawler.archive import AUTO_COMPACT_SEGMENTS, Archive
from image_crawler.config import load_config
from image_crawler.crawler import Crawler, CrawlStats
from image_crawler.crypto import KIND_IMAGE, DecryptionError, Vault, generate_key_file
from image_crawler.doctor import explain, run_checks
from image_crawler.quota import (
    GB,
    FREE_TIER,
    GuardedStorage,
    QuotaExceeded,
    Usage,
    billing_period,
    measure,
    soft_limits,
)
from image_crawler.sites import add_sites, read_sites, remove_sites
from image_crawler.state import State
from image_crawler.statesync import StateSync
from image_crawler.storage import Storage, build_storage
from image_crawler.storage.s3 import S3Storage
from image_crawler.viewer import MIN_PASSPHRASE, make_bundle, seal_bundle

EXIT_QUOTA = 3

log = logging.getLogger("image_crawler")


def cmd_keygen(args, cfg) -> int:
    path = Path(args.key_file).expanduser() if args.key_file else cfg.key_file
    generate_key_file(path, overwrite=args.force)
    print(f"created key: {path}")
    print("Back this file up somewhere safe and offline. Without it the stored images cannot be decrypted.")
    return 0


def _sites_session(cfg):
    """With state_sync, site list changes are pulled first and pushed back like crawl state."""
    return _session(cfg, storage=False) if cfg.state_sync else nullcontext()


def cmd_add(args, cfg) -> int:
    with _sites_session(cfg):
        added = add_sites(cfg.sites_file, args.urls)
    for url in added:
        print(f"added {url}")
    if not added:
        print("nothing new to add")
    return 0


def cmd_remove(args, cfg) -> int:
    with _sites_session(cfg):
        removed = remove_sites(cfg.sites_file, args.urls)
    for url in removed:
        print(f"removed {url}")
    if not removed:
        print("no matching sites")
    return 0


def cmd_sites(args, cfg) -> int:
    with _sites_session(cfg):
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


@contextmanager
def _session(cfg, *, storage: bool = True):
    """Open the crawl state and (optionally) the quota-guarded storage for one command.

    With state_sync, the state is first pulled from the storage and, when the command is
    done (even if it failed), pushed back if it changed. Yields (state, storage or None).
    """
    sync = None
    if cfg.state_sync:
        sync = StateSync(Vault.load(cfg.key_file), build_storage(cfg.storage), cfg.state_db, cfg.sites_file)
        sync.pull()
    state = State(cfg.state_db)
    try:
        if sync:
            _meter_pull(cfg, state, sync)
        opened = _open_storage(cfg, state) if storage else None
        if sync:
            sync.begin(state)
        try:
            yield state, opened
        finally:
            if sync:
                _push_state(cfg, sync, state, opened)
    finally:
        state.close()


def _meter_pull(cfg, state: State, sync: StateSync) -> None:
    """Count pull()'s requests (made before the meter could be opened) toward the quota."""
    if _quota_applies(cfg, sync.storage):
        start, _ = billing_period(datetime.now(timezone.utc).date(), cfg.quota.billing_day)
        state.add_ops(sync.storage.describe(), start.isoformat(), class_b=sync.pull_requests)


def _push_state(cfg, sync: StateSync, state: State, storage: Storage | None) -> None:
    try:
        if not sync.changed(state):
            return
        generation = sync.push(state, storage or _open_storage(cfg, state))
    except Exception as e:
        print(f"warning: crawl state not backed up to storage: {explain(e)}", file=sys.stderr)
        return
    print(f"crawl state backed up to storage (generation {generation})")


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
    if args.max_depth is not None:
        cfg.crawl.max_depth = args.max_depth
    if args.max_pages is not None:
        cfg.crawl.max_pages_per_site = args.max_pages

    with _session(cfg) as (state, storage):
        # Read after the session opens: with state_sync the site list comes from the storage.
        sites = args.site or read_sites(cfg.sites_file)
        if not sites:
            print(f"no sites to crawl; add some with `image-crawler add URL` ({cfg.sites_file})")
            return 1
        archive = Archive(Vault.load(cfg.key_file), storage)
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
        if not crawler.stop_reason:
            _auto_compact(archive)
    if len(sites) > 1:
        print(f"total: {_format_stats(total)}")
    return EXIT_QUOTA if crawler.stop_reason else 0


def _auto_compact(archive: Archive) -> None:
    """Keep the number of index segments (what the phone downloads on refresh) small."""
    try:
        if len(archive.segment_keys()) > AUTO_COMPACT_SEGMENTS:
            before, after = archive.compact()
            print(f"compacted index: {before} segments -> {after}")
    except QuotaExceeded as e:
        log.warning("index compaction skipped: %s", e)


def cmd_compact(args, cfg) -> int:
    try:
        with _session(cfg) as (_, storage):
            before, after = Archive(Vault.load(cfg.key_file), storage).compact()
    except QuotaExceeded as e:
        print(f"stopped: {e}")
        return EXIT_QUOTA
    print(f"index segments: {before} -> {after}")
    return 0


def _format_stats(s: CrawlStats) -> str:
    text = (
        f"{s.pages} page(s), {s.images_found} image(s) found, {s.stored} stored, "
        f"{s.duplicates} duplicate(s), {s.already_seen} already seen, "
        f"{s.skipped} skipped, {s.errors} error(s)"
    )
    if s.albums_finished or s.albums_done:
        text += f"; albums: {s.albums_finished} finished, {s.albums_done} done earlier (not fetched)"
    return text


_UNSAFE = re.compile(r"[^\w.\-]+")


def _format_bytes(value: float) -> str:
    """GB with two decimals, or MB below 1 GB so a small vault doesn't read as "0.00 GB"."""
    return f"{value / GB:.2f} GB" if value >= GB else f"{value / 1_000_000:.1f} MB"


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
    vault = Vault.load(cfg.key_file)
    if args.from_dir:
        return _restore(args, vault, _blobs_in_dir(Path(args.from_dir).expanduser()))
    with _session(cfg) as (_, storage):
        archive = Archive(vault, storage)
        print(f"restoring from {storage.describe()}")
        items = (
            (n, (lambda n=n: storage.get(layout.image_key(n))))
            for n in archive.image_names()
        )
        return _restore(args, vault, items)


def _restore(args, vault: Vault, items) -> int:
    out_dir = Path(args.out).expanduser()
    restored = failed = existing = 0
    for name, load in items:
        if args.limit is not None and restored >= args.limit:
            print(f"stopping after --limit {args.limit}")
            break
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
        target = out_dir / site_dir
        if meta.get("album"):  # one folder per album (page title), photos in page order
            target /= _safe_part(meta["album"], "album")
            if isinstance(meta.get("position"), int):
                filename = f"{meta['position']:03d}_{filename}"
        target /= filename
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
    with _session(cfg) as (_, storage):
        return _reindex(Archive(Vault.load(cfg.key_file), storage))


def _reindex(archive: Archive) -> int:
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
    with _session(cfg, storage=False) as (state, _):
        report = measure(cfg.quota, state, storage.describe())

    limits, usage, cloud = soft_limits(cfg.quota), report.effective, report.cloudflare
    print(f"R2 usage {report.period_start} ~ {report.period_end} (UTC), {storage.describe()}")
    print(f"  {'':8}{'this machine':>14}{'cloudflare':>14}{'limit':>14}{'used':>7}{'free tier':>14}")
    for label, attr, fmt in (
        ("storage", "storage_bytes", _format_bytes),
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


def cmd_check(args, cfg) -> int:
    ok = run_checks(cfg, lambda state: _open_storage(cfg, state))
    return 0 if ok else 1


def _secret(env: str, prompt: str) -> str:
    value = os.environ.get(env) or getpass.getpass(prompt)
    if not value:
        raise ValueError(f"{prompt.rstrip(': ')} is required (or set {env})")
    return value


def _passphrase() -> str:
    if env := os.environ.get("IMAGE_CRAWLER_VIEWER_PASSPHRASE"):
        return env
    first = getpass.getpass(f"Viewer passphrase (min {MIN_PASSPHRASE} chars, typed on the phone): ")
    if getpass.getpass("Repeat passphrase: ") != first:
        raise ValueError("passphrases do not match")
    return first


def verify_read_only(client, bucket: str, prefix: str) -> None:
    """The phone's token must be able to list/read the bucket and must NOT be able to write."""
    from botocore.exceptions import ClientError

    client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    probe = f"{prefix}check/viewer-write-probe-{secrets.token_hex(6)}.bin"
    try:
        client.put_object(Bucket=bucket, Key=probe, Body=b"")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("AccessDenied", "Unauthorized", "403"):
            return
        raise
    client.delete_object(Bucket=bucket, Key=probe)
    raise ValueError(
        "the viewer token can WRITE to the bucket; create an R2 token with 'Object Read only' for the phone"
    )


def cmd_viewer_export(args, cfg) -> int:
    """Seal what the phone viewer needs into a passphrase-protected bundle (text and QR)."""
    if cfg.storage.get("backend") != "s3" or not cfg.storage.get("s3", {}).get("endpoint_url"):
        print("viewer-export needs the s3 backend with endpoint_url set (e.g. Cloudflare R2)", file=sys.stderr)
        return 2
    s3 = cfg.storage["s3"]
    vault = Vault.load(cfg.key_file)
    key_id = _secret("IMAGE_CRAWLER_VIEWER_KEY_ID", "Viewer (read-only) R2 access key id: ")
    secret = _secret("IMAGE_CRAWLER_VIEWER_SECRET", "Viewer (read-only) R2 secret access key: ")
    bundle = make_bundle(
        endpoint=s3["endpoint_url"],
        region=s3.get("region") or "auto",
        bucket=s3["bucket"],
        prefix=s3.get("prefix", ""),
        access_key_id=key_id,
        secret_access_key=secret,
        encryption_key=vault.encryption_key,
    )
    if not args.no_verify:
        import boto3

        client = boto3.client(
            "s3", endpoint_url=bundle["e"], region_name=bundle["r"],
            aws_access_key_id=key_id, aws_secret_access_key=secret,
        )
        try:
            verify_read_only(client, bundle["b"], bundle["p"])
        except Exception as e:
            print(f"viewer token check failed: {explain(e)}", file=sys.stderr)
            return 1
        print("✓ viewer token can read the bucket and cannot write to it")

    text = seal_bundle(bundle, _passphrase())
    if args.out:
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text + "\n")
        print(f"bundle written to {args.out}")
    if args.qr_png:
        import qrcode

        qrcode.make(text).save(args.qr_png)
        print(f"QR code written to {args.qr_png}")
    if not args.out and not args.qr_png:
        try:
            import qrcode

            qr = qrcode.QRCode(border=2)
            qr.add_data(text)
            qr.print_ascii(invert=True)
        except ImportError:
            print("(install 'qrcode' to show a QR code here)")
        print(text)
    print("Scan or paste this in the viewer, then enter the passphrase. Delete any saved copy afterwards.")
    return 0


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
    p.add_argument("--limit", type=int, help="restore at most this many images (saves downloads)")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("compact", help="merge index segments so the viewer loads faster")
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("reindex", help="add thumbnails/index entries for images missing from the index")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("cors", help="allow the PWA viewer's origin to read the bucket (s3/R2 only)")
    p.add_argument("--origin", action="append", required=True,
                   help="viewer origin, e.g. https://you.github.io (repeatable)")
    p.set_defaults(func=cmd_cors)

    p = sub.add_parser("check", help="verify key, credentials and bucket access step by step")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("viewer-export", help="create the passphrase-protected bundle for the phone viewer")
    p.add_argument("--out", help="write the bundle text to this file (mode 600) instead of printing it")
    p.add_argument("--qr-png", help="also save the QR code as a PNG")
    p.add_argument("--no-verify", action="store_true", help="skip checking that the viewer token is read-only")
    p.set_defaults(func=cmd_viewer_export)

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
