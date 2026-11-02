"""Crawl registered sites, collect image URLs, and hand each image to the vault."""

from __future__ import annotations

import logging
import mimetypes
import posixpath
import heapq
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib import robotparser
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from image_crawler.archive import Archive
from image_crawler.config import CrawlSettings
from image_crawler.quota import QuotaExceeded
from image_crawler.state import State

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


@dataclass
class Page:
    images: list[str]  # absolute image URLs, in page order
    links: list[str]   # absolute links to other pages
    title: str         # folder name for the page's images


def _text(tag) -> str:
    return " ".join(tag.get_text(" ", strip=True).split()) if tag is not None else ""


def page_title(soup: BeautifulSoup, selector: str = "") -> str:
    """The folder name for a page: `selector`'s text, else the first <h1>, else <title>."""
    if selector and (title := _text(soup.select_one(selector))):
        return title[:200]
    return (_text(soup.find("h1")) or _text(soup.find("title")))[:200]


def extract_page(
    html: str, base_url: str, *, image_selector: str = "", title_selector: str = ""
) -> Page:
    """Images, page links and the folder title of an HTML document (URLs absolute).

    With `image_selector`, only the matching <img>/<source>/<picture> elements count as the
    page's images (no og:image or linked image files): an album page's own photos, not the
    covers of other albums around them.
    """
    soup = BeautifulSoup(html, "html.parser")
    if (base := soup.find("base", href=True)) is not None:
        base_url = urljoin(base_url, base["href"])

    if image_selector:
        tags = []
        for tag in soup.select(image_selector):
            tags.extend([tag] if tag.name in ("img", "source") else tag.find_all(["img", "source"]))
    else:
        tags = soup.find_all(["img", "source"])

    raw_images: list[str] = []
    for tag in tags:
        if tag.name == "source" and tag.find_parent("picture") is None:
            continue  # <source> of <video>/<audio>
        for attr in SRCSET_ATTRS:
            if tag.get(attr) and (best := _best_from_srcset(tag[attr])):
                raw_images.append(best)
        for attr in LAZY_SRC_ATTRS:
            if tag.get(attr):
                raw_images.append(tag[attr])
    if not image_selector:
        for meta in soup.find_all("meta", content=True):
            key = (meta.get("property") or meta.get("name") or "").lower()
            if key in META_IMAGE_KEYS:
                raw_images.append(meta["content"])

    raw_links: list[str] = []
    for a in soup.find_all("a", href=True):
        if _extension(urljoin(base_url, a["href"])) in IMAGE_EXTENSIONS:
            if not image_selector:
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

    return Page(absolutize(raw_images), absolutize(raw_links), page_title(soup, title_selector))


BOUNDARY_SLACK = 20  # list pages to look past the target for an album that new albums pushed back
CHECKPOINT_ALBUMS = 20
CHECKPOINT_SECONDS = 300


@dataclass
class ListingPlan:
    """Where a numbered-list crawl stops (see SiteRule.list_pages).

    `to_page` is the requested last list page. `boundary` is the last album that page showed
    when it was first read: albums added later push it to a later page, so a run that
    continues the same target goes on until the page where the boundary album is now.
    """

    to_page: int | None = None
    boundary: str | None = None
    max_albums: int | None = None  # stop after finishing this many albums in this run
    # Filled in while crawling:
    boundary_page: int | None = None  # where the boundary album was found this run
    page_albums: dict[int, list[str]] = field(default_factory=dict)  # list page -> its albums

    def allows(self, number: int) -> bool:
        if self.to_page is None:
            return True
        if self.boundary:
            if self.boundary_page is not None:
                return number <= self.boundary_page
            return number <= self.to_page + BOUNDARY_SLACK
        return number <= self.to_page

    def saw_list_page(self, number: int, albums: list[str]) -> None:
        self.page_albums[number] = albums
        if self.boundary and self.boundary in albums and self.boundary_page is None:
            self.boundary_page = number
        elif not self.boundary and number == self.to_page and albums:
            self.boundary = albums[-1]
            self.boundary_page = number


@dataclass
class CrawlStats:
    pages: int = 0
    images_found: int = 0
    stored: int = 0
    duplicates: int = 0
    already_seen: int = 0
    skipped: int = 0
    errors: int = 0
    albums_done: int = 0     # album pages finished in an earlier run, not fetched again
    albums_finished: int = 0
    regrouped: int = 0       # images found earlier on a non-album page, now filed under their album

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
        archive: Archive,
        state: State,
        fetcher: Fetcher | None = None,
    ):
        self.settings = settings
        self.archive = archive
        self.state = state
        self.stop_reason: str | None = None  # set when a quota stops the run
        self.redo_albums = False  # open finished albums again (e.g. after a rule now finds more photos)
        self.plan = ListingPlan()  # numbered-list limits for the next crawl_site()
        self.checkpoint = None  # called now and then during long runs (saves the crawl state)
        self._last_checkpoint = time.monotonic()
        self.fetcher = fetcher or Fetcher(settings)

    def crawl_site(self, site_url: str) -> CrawlStats:
        """Breadth-first crawl of one site, staying on its host, storing every image found."""
        stats = CrawlStats()
        try:
            self._crawl(site_url, stats)
        except QuotaExceeded as e:
            self.stop_reason = str(e)
            log.warning("stopping: %s", e)
        finally:
            try:
                self.archive.flush()
            except Exception as e:
                log.error("index upload failed (run `image-crawler reindex` later): %s", e)
                stats.errors += 1
        return stats

    def _crawl(self, site_url: str, stats: CrawlStats) -> None:
        site_host = _host(site_url)
        rule = self.settings.rule_for(site_url)
        plan = self.plan
        max_depth = rule.max_depth if rule and rule.max_depth is not None else self.settings.max_depth
        queue: deque[tuple[str, int]] = deque([(site_url, 0)])
        # Numbered list pages wait here and are read in page order, each one only after the
        # albums of the pages before it.
        list_queue: list[tuple[int, str, int]] = []
        visited: set[str] = set()

        def next_page() -> tuple[str, int] | None:
            if queue:
                return queue.popleft()
            while list_queue:
                number, url, depth = heapq.heappop(list_queue)
                if plan.allows(number):
                    return url, depth
            return None

        while stats.pages < self.settings.max_pages_per_site:
            if plan.max_albums is not None and stats.albums_finished >= plan.max_albums:
                break
            item = next_page()
            if item is None:
                break
            page_url, depth = item
            if page_url in visited:
                continue
            visited.add(page_url)
            is_album = rule is not None and rule.is_album(page_url)
            if is_album and not self.redo_albums and self.state.album_done(page_url):
                stats.albums_done += 1
                continue
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
            page = extract_page(
                resp.text,
                resp.url,
                image_selector=rule.album_images if is_album and rule else "",
                title_selector=rule.title if rule else "",
            )
            if is_album and rule and rule.album_list:
                if listed := rule.listed_images(resp.text, resp.url):
                    page.images = listed
                else:
                    log.warning("album list not found on %s; using the images shown on the page", page_url)
            log.info("page %s: %d image(s), %d link(s)", page_url, len(page.images), len(page.links))
            if rule and (number := rule.list_page_number(page_url)) is not None:
                plan.saw_list_page(number, self._albums_on(page.links, rule))
                if plan.to_page is not None and not plan.boundary and number != plan.to_page:
                    self._read_target_page(rule, page_url, plan)

            if is_album or rule is None or rule.other_images:
                errors_before = stats.errors
                for position, image_url in enumerate(page.images, 1):
                    stats.images_found += 1
                    if is_album and self._regroup(image_url, page_url, site_url, rule, page.title, position):
                        stats.regrouped += 1
                    self._process_image(image_url, page_url, site_url, stats, page.title, position)
                if is_album and stats.errors == errors_before:
                    self.state.finish_album(page_url, page.title, len(page.images))
                    stats.albums_finished += 1
                    self._maybe_checkpoint(stats)

            if depth < max_depth:
                for link in page.links:
                    if (
                        _host(link) == site_host
                        and link not in visited
                        and _extension(link) not in NON_PAGE_EXTENSIONS
                        and (rule is None or rule.follows(link))
                    ):
                        number = rule.list_page_number(link) if rule else None
                        if number is not None:
                            heapq.heappush(list_queue, (number, link, depth + 1))
                        else:
                            queue.append((link, depth + 1))

    @staticmethod
    def _albums_on(links: list[str], rule) -> list[str]:
        return list(dict.fromkeys(link for link in links if rule.is_album(link)))

    def _read_target_page(self, rule, list_url: str, plan: ListingPlan) -> None:
        """Note which album ends a new target right away, so a run cut short before reaching
        that page can still be resumed to the same place after newer albums push it back."""
        url = rule.list_page_url(list_url, plan.to_page)
        if url is None or not self.fetcher.allowed(url):
            return
        try:
            resp = self.fetcher.get(url)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("could not read target list page %s: %s", url, e)
            return
        albums = self._albums_on(extract_page(resp.text, resp.url).links, rule)
        if albums:
            plan.boundary, plan.boundary_page = albums[-1], plan.to_page
        # else: past the last page, so the target is simply every page there is

    def _maybe_checkpoint(self, stats: CrawlStats) -> None:
        """Save progress now and then, so a long run that dies loses little."""
        if self.checkpoint is None:
            return
        due = time.monotonic() - self._last_checkpoint >= CHECKPOINT_SECONDS
        if stats.albums_finished % CHECKPOINT_ALBUMS and not due:
            return
        self._last_checkpoint = time.monotonic()
        try:
            self.archive.flush()  # index entries before the state that says they're done
            self.checkpoint()
        except QuotaExceeded:
            raise
        except Exception as e:
            log.warning("checkpoint failed (will retry later): %s", e)

    def _regroup(self, url: str, page_url: str, site_url: str, rule, album: str, position: int) -> bool:
        """File an image stored earlier from a non-album page (e.g. a list page's cover) under
        this album: a new index entry replaces the old one (readers keep the latest)."""
        record = self.state.url_record(url)
        if record is None:
            return False
        name, previous_page, status = record
        if not name or status not in ("stored", "duplicate") or previous_page == page_url:
            return False
        if rule.is_album(previous_page):
            return False  # the same photo in two albums stays in the first
        try:
            opened = self.archive.open_image(name)
            metadata = {**opened.metadata, "page_url": page_url, "site": site_url, "album": album or None,
                        "position": position, "fetched_at": datetime.now(timezone.utc).isoformat()}
            self.archive.add_to_index(name, opened.content, metadata)
        except QuotaExceeded:
            raise
        except Exception as e:
            log.warning("could not move %s into its album: %s", url, e)
            return False
        self.state.record_url(url, status, name, page_url, site_url)
        return True

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

    def _process_image(
        self,
        url: str,
        page_url: str,
        site_url: str,
        stats: CrawlStats,
        album: str = "",
        position: int | None = None,
    ) -> None:
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
        name = self.archive.vault.object_name(data)
        if self.state.has_object(name) or self.archive.has_image(name):
            self.state.add_object(name)
            self.state.record_url(url, "duplicate", name, page_url, site_url)
            stats.duplicates += 1
            return

        metadata = {
            "source_url": url,
            "page_url": page_url,
            "site": site_url,
            "album": album or None,
            "position": position,
            "filename": _filename_for(url, mime),
            "content_type": mime,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.archive.store(data, metadata)
        except QuotaExceeded:
            raise
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
