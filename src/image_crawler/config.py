from __future__ import annotations

import copy
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_FILE = "config.toml"


@dataclass
class SiteRule:
    """How to crawl one site whose albums sit behind list pages (`[[crawl.rules]]`).

    Patterns are regular expressions searched in a link's path (plus `?query`), so
    `'^/album/\\d+$'` matches `https://host/album/123`.
    """

    host: str                    # sites on this host (a leading "www." is ignored)
    follow: list[str] = field(default_factory=list)  # only follow matching links (empty = all on the host)
    album_pages: str = ""        # pages that are albums; an album finished once isn't fetched again
    album_images: str = ""       # CSS selector for an album page's own images (empty = every image on it)
    # Album pages that show only the first photos and reveal the rest with a "More" button
    # usually carry the whole list in the page's data. album_list is a regex searched in the
    # page source whose group 1 (or whole match) holds that list; album_item picks each entry
    # out of it; album_url turns an entry into an image URL ("{}" = the entry).
    album_list: str = ""
    album_item: str = ""
    album_url: str = ""
    title: str = ""              # CSS selector for the folder name (empty = the first <h1>, else <title>)
    other_images: bool = True    # also store images found on pages that aren't albums
    max_depth: int | None = None  # overrides crawl.max_depth (list pages chain, so it may need to be large)

    def __post_init__(self):
        self.host = self.host.strip().lower().removeprefix("www.")
        if not self.host:
            raise ValueError("[[crawl.rules]] needs host")
        if isinstance(self.follow, str):
            self.follow = [self.follow]
        if self.album_list and not self.album_item:
            raise ValueError(f"[[crawl.rules]] {self.host}: album_list needs album_item")
        for pattern in [*self.follow, self.album_pages, self.album_list, self.album_item]:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"[[crawl.rules]] {self.host}: bad pattern {pattern!r}: {e}") from None
        for selector in (self.album_images, self.title):
            if selector:
                import soupsieve

                try:
                    soupsieve.compile(selector)
                except Exception as e:
                    raise ValueError(f"[[crawl.rules]] {self.host}: bad CSS selector {selector!r}: {e}") from None

    def _matches(self, pattern: str, url: str) -> bool:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        return re.search(pattern, target) is not None

    def follows(self, url: str) -> bool:
        return not self.follow or any(self._matches(p, url) for p in self.follow)

    def listed_images(self, html: str, base_url: str) -> list[str]:
        """Image URLs from the album's own list in the page source (empty if not found)."""
        from urllib.parse import urljoin

        if not self.album_list or not (match := re.search(self.album_list, html, re.S)):
            return []
        text = match.group(1) if match.re.groups else match.group(0)
        urls = []
        for found in re.finditer(self.album_item, text):
            item = (found.group(1) if found.re.groups else found.group(0)).replace("\\/", "/")
            urls.append(self.album_url.replace("{}", item) if self.album_url else urljoin(base_url, item))
        return list(dict.fromkeys(urls))

    def is_album(self, url: str) -> bool:
        return bool(self.album_pages) and self._matches(self.album_pages, url)


@dataclass
class CrawlSettings:
    max_depth: int = 1
    max_pages_per_site: int = 50
    delay_seconds: float = 1.0
    timeout_seconds: float = 20.0
    max_retries: int = 2
    min_image_bytes: int = 5_000
    max_image_bytes: int = 50_000_000
    respect_robots_txt: bool = True
    user_agent: str = "image-crawler/0.1"
    rules: list[SiteRule] = field(default_factory=list)

    def rule_for(self, site_url: str) -> SiteRule | None:
        from urllib.parse import urlsplit

        host = (urlsplit(site_url).hostname or "").removeprefix("www.")
        return next((r for r in self.rules if r.host == host), None)


@dataclass
class QuotaSettings:
    """Stay inside the Cloudflare R2 free tier (10 GB-month, 1M Class A, 10M Class B per month).

    Applies to the s3 backend. Limits are soft: kept below the free tier to leave
    headroom for usage this machine can't see in real time (e.g. the phone viewer).
    """

    enabled: bool = True
    storage_limit_gb: float = 9.0
    class_a_limit: int = 900_000
    class_b_limit: int = 9_000_000
    max_images_per_run: int = 2_000
    warn_percent: int = 80
    billing_day: int = 1
    # Optional: read real account-wide usage from Cloudflare. The API token (Account
    # Analytics: Read) comes from the CLOUDFLARE_API_TOKEN environment variable.
    cloudflare_account_id: str = ""


def _section(cls, data: dict, name: str):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown [{name}] option(s): {', '.join(sorted(unknown))}")
    return cls(**data)


@dataclass
class Config:
    sites_file: Path
    state_db: Path
    key_file: Path
    crawl: CrawlSettings
    storage: dict = field(default_factory=dict)
    quota: QuotaSettings = field(default_factory=QuotaSettings)
    # Keep the crawl history (state_db) and the site list in the storage too, encrypted,
    # so a machine that starts empty every time (a cloud runner) picks up where it left off.
    state_sync: bool = False


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def load_config(path: str | Path | None = None) -> Config:
    """Load a TOML config. Relative paths are resolved against the config file's directory."""
    config_path = Path(path or DEFAULT_CONFIG_FILE).expanduser()
    if config_path.exists():
        data = tomllib.loads(config_path.read_text())
        base = config_path.resolve().parent
    elif path is not None:
        raise FileNotFoundError(f"config file not found: {config_path}")
    else:
        data, base = {}, Path.cwd()

    storage = copy.deepcopy(data.get("storage", {}))
    storage.setdefault("backend", "local")
    if storage["backend"] == "local":
        local = storage.setdefault("local", {})
        local["path"] = str(_resolve(base, local.get("path", "vault")))

    return Config(
        sites_file=_resolve(base, data.get("sites_file", "sites.txt")),
        state_db=_resolve(base, data.get("state_db", "~/.image-crawler/state.db")),
        key_file=_resolve(base, data.get("key_file", "~/.image-crawler/key")),
        crawl=_crawl_settings(data.get("crawl", {})),
        storage=storage,
        quota=_section(QuotaSettings, data.get("quota", {}), "quota"),
        state_sync=_flag(data, "state_sync"),
    )


def _crawl_settings(data: dict) -> CrawlSettings:
    data = dict(data)
    rules = data.pop("rules", [])
    if not isinstance(rules, list):
        raise ValueError("crawl.rules must be written as [[crawl.rules]] tables")
    settings = _section(CrawlSettings, data, "crawl")
    settings.rules = [_section(SiteRule, rule, "crawl.rules") for rule in rules]
    return settings


def _flag(data: dict, name: str) -> bool:
    value = data.get(name, False)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value
