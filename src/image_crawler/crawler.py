"""Crawl registered sites, collect image URLs, and hand each image to the vault."""

from __future__ import annotations

import logging
import mimetypes
import posixpath
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib import robotparser
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from image_crawler.config import CrawlSettings
from image_crawler.crypto import Vault
from image_crawler.state import State
from image_crawler.storage import Storage

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg", ".tif", ".tiff"}
NON_PAGE_EXTENSIONS = IMAGE_EXTENSIONS | {
    ".pdf", ".zip", ".gz", ".rar", ".7z", ".mp4", ".webm", ".mp3", ".css", ".js", ".json", ".xml", ".ico",
}
LAZY_SRC_ATTRS = ("src", "data-src", "data-original", "data-lazy-src", "data-url")
SRCSET_ATTRS = ("srcset", "data-srcset")
META_IMAGE_KEYS = {"og:image", "og:image:url", "og:image:secure_url", "twitter:image"}
RETRY_STATUSES = {429, 503}

_MAGIC_TYPES = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
]


def sniff_image_type(data: bytes) -> str | None:
    for magic, mime in _MAGIC_TYPES:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    head = data[:1024].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "image/svg+xml"
    return None


def _extension(url: str) -> str:
    return posixpath.splitext(urlsplit(url).path)[1].lower()


def _host(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _best_from_srcset(srcset: str) -> str | None:
    """Pick the largest candidate (by `w` or `x` descriptor) from a srcset."""
    best, best_score = None, -1.0
    for candidate in srcset.split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        score = 1.0
        if len(parts) > 1:
            try:
                score = float(parts[1][:-1]) if parts[1][-1] in "wx" else 1.0
            except ValueError:
                pass
        if score > best_score:
            best, best_score = parts[0], score
    return best


def extract_page(html: str, base_url: str) -> tuple[list[str], list[str]]:
    """Return (image URLs, page links) found in an HTML document, both absolute."""
    soup = BeautifulSoup(html, "html.parser")
    if (base := soup.find("base", href=True)) is not None:
        base_url = urljoin(base_url, base["href"])

    raw_images: list[str] = []
    for tag in soup.find_all(["img", "source"]):
        if tag.name == "source" and tag.find_parent("picture") is None:
            continue  # <source> of <video>/<audio>
        for attr in SRCSET_ATTRS:
            if tag.get(attr) and (best := _best_from_srcset(tag[attr])):
                raw_images.append(best)
        for attr in LAZY_SRC_ATTRS:
            if tag.get(attr):
                raw_images.append(tag[attr])
    for meta in soup.find_all("meta", content=True):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key in META_IMAGE_KEYS:
            raw_images.append(meta["content"])

    raw_links: list[str] = []
    for a in soup.find_all("a", href=True):
        if _extension(urljoin(base_url, a["href"])) in IMAGE_EXTENSIONS:
            raw_images.append(a["href"])
        else:
            raw_links.append(a["href"])

    def absolutize(values: list[str]) -> list[str]:
        out = []
        for value in values:
            value = value.strip()
            if not value or value.startswith(("data:", "javascript:", "mailto:", "tel:", "#")):
                continue
            url = urldefrag(urljoin(base_url, value)).url
            if urlsplit(url).scheme in ("http", "https"):
                out.append(url)
        return list(dict.fromkeys(out))

    return absolutize(raw_images), absolutize(raw_links)


@dataclass
class CrawlStats:
    pages: int = 0
    images_found: int = 0
    stored: int = 0
    duplicates: int = 0
    already_seen: int = 0
    skipped: int = 0
    errors: int = 0

    def __iadd__(self, other: "CrawlStats") -> "CrawlStats":
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        return self


class Fetcher:
    """HTTP client that honours robots.txt and a per-host delay between requests."""

    def __init__(self, settings: CrawlSettings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = settings.user_agent
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._last_request: dict[str, float] = {}

    def _robots_for(self, url: str) -> robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = robotparser.RobotFileParser()
            try:
                self._wait(origin)
                resp = self.session.get(origin + "/robots.txt", timeout=self.settings.timeout_seconds)
                if resp.status_code in (401, 403):
                    parser.disallow_all = True
                elif resp.ok:
                    parser.parse(resp.text.splitlines())
                else:
                    parser.allow_all = True
            except requests.RequestException as e:
                log.warning("could not fetch robots.txt for %s (%s); assuming allowed", origin, e)
                parser.allow_all = True
            self._robots[origin] = parser
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        if not self.settings.respect_robots_txt:
            return True
        parser = self._robots_for(url)
        return parser is None or parser.can_fetch(self.settings.user_agent, url)

    def _delay_for(self, origin: str) -> float:
        delay = self.settings.delay_seconds
        parser = self._robots.get(origin) if self.settings.respect_robots_txt else None
        if parser is not None:
            crawl_delay = parser.crawl_delay(self.settings.user_agent)
            if crawl_delay:
                delay = max(delay, float(crawl_delay))
        return delay

    def _wait(self, origin: str) -> None:
        last = self._last_request.get(origin)
        if last is not None:
            remaining = self._delay_for(origin) - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[origin] = time.monotonic()

    def get(self, url: str, **kwargs) -> requests.Response:
        """GET with the per-host delay; backs off and retries on 429/503."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        for attempt in range(self.settings.max_retries + 1):
            self._wait(origin)
            resp = self.session.get(url, timeout=self.settings.timeout_seconds, **kwargs)
            if resp.status_code not in RETRY_STATUSES or attempt == self.settings.max_retries:
                return resp
            retry_after = resp.headers.get("Retry-After", "")
            backoff = float(retry_after) if retry_after.isdigit() else 2.0 * 2**attempt
            resp.close()
            log.info("%s returned %d; retrying in %.0fs", url, resp.status_code, backoff)
            time.sleep(min(backoff, 120.0))
        raise AssertionError("unreachable")


class Crawler:
    def __init__(
        self,
        settings: CrawlSettings,
        vault: Vault,
        storage: Storage,
        state: State,
        fetcher: Fetcher | None = None,
    ):
        self.settings = settings
        self.vault = vault
        self.storage = storage
        self.state = state
        self.fetcher = fetcher or Fetcher(settings)

    def crawl_site(self, site_url: str) -> CrawlStats:
        """Breadth-first crawl of one site, staying on its host, storing every image found."""
        stats = CrawlStats()
        site_host = _host(site_url)
        queue: deque[tuple[str, int]] = deque([(site_url, 0)])
        visited: set[str] = set()

        while queue and stats.pages < self.settings.max_pages_per_site:
            page_url, depth = queue.popleft()
            if page_url in visited:
                continue
            visited.add(page_url)
            if not self.fetcher.allowed(page_url):
                log.info("robots.txt disallows %s", page_url)
                continue
            try:
                resp = self.fetcher.get(page_url)
                resp.raise_for_status()
            except requests.RequestException as e:
                log.warning("page failed %s: %s", page_url, e)
                stats.errors += 1
                continue
            if "html" not in resp.headers.get("Content-Type", "text/html").lower():
                continue
            stats.pages += 1
            images, links = extract_page(resp.text, resp.url)
            log.info("page %s: %d image(s), %d link(s)", page_url, len(images), len(links))

            for image_url in images:
                stats.images_found += 1
                self._process_image(image_url, page_url, site_url, stats)

            if depth < self.settings.max_depth:
                for link in links:
                    if (
                        _host(link) == site_host
                        and link not in visited
                        and _extension(link) not in NON_PAGE_EXTENSIONS
                    ):
                        queue.append((link, depth + 1))
        return stats

    def _download_image(self, url: str) -> tuple[bytes, str] | None:
        """Fetch an image, enforcing size limits. Returns (bytes, mime) or None if skipped."""
        limit = self.settings.max_image_bytes
        with self.fetcher.get(url, stream=True) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                log.info("skip %s: %s bytes exceeds limit", url, declared)
                return None
            chunks, size = [], 0
            for chunk in resp.iter_content(64 * 1024):
                size += len(chunk)
                if size > limit:
                    log.info("skip %s: larger than %d bytes", url, limit)
                    return None
                chunks.append(chunk)
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        data = b"".join(chunks)
        if len(data) < self.settings.min_image_bytes:
            log.debug("skip %s: only %d bytes", url, len(data))
            return None
        mime = sniff_image_type(data) or (content_type if content_type.startswith("image/") else None)
        if mime is None:
            log.info("skip %s: not an image (%s)", url, content_type or "no content type")
            return None
        return data, mime

    def _process_image(self, url: str, page_url: str, site_url: str, stats: CrawlStats) -> None:
        if self.state.seen_url(url):
            stats.already_seen += 1
            return
        if not self.fetcher.allowed(url):
            log.info("robots.txt disallows %s", url)
            stats.skipped += 1
            return
        try:
            result = self._download_image(url)
        except requests.RequestException as e:
            log.warning("image failed %s: %s", url, e)
            stats.errors += 1
            return
        if result is None:
            self.state.record_url(url, "skipped", None, page_url, site_url)
            stats.skipped += 1
            return
        data, mime = result
        name = self.vault.object_name(data)
        if self.state.has_object(name) or self.storage.exists(name):
            self.state.add_object(name)
            self.state.record_url(url, "duplicate", name, page_url, site_url)
            stats.duplicates += 1
            return

        metadata = {
            "source_url": url,
            "page_url": page_url,
            "site": site_url,
            "filename": _filename_for(url, mime),
            "content_type": mime,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.storage.put(name, self.vault.seal(data, metadata))
        except Exception as e:
            log.error("upload failed for %s: %s", url, e)
            stats.errors += 1
            return
        self.state.add_object(name)
        self.state.record_url(url, "stored", name, page_url, site_url)
        stats.stored += 1
        log.info("stored %s (%d bytes)", url, len(data))


def _filename_for(url: str, mime: str) -> str:
    base = posixpath.basename(unquote(urlsplit(url).path)) or "image"
    stem, ext = posixpath.splitext(base)
    if ext.lower() not in IMAGE_EXTENSIONS:
        ext = mimetypes.guess_extension(mime) or ".img"
        stem = base
    return f"{stem}{ext}"
