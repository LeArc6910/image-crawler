from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_FILE = "config.toml"


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
        crawl=_section(CrawlSettings, data.get("crawl", {}), "crawl"),
        storage=storage,
        quota=_section(QuotaSettings, data.get("quota", {}), "quota"),
        state_sync=_flag(data, "state_sync"),
    )


def _flag(data: dict, name: str) -> bool:
    value = data.get(name, False)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value
