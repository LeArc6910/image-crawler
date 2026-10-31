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

    @classmethod
    def from_dict(cls, data: dict) -> "CrawlSettings":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown [crawl] option(s): {', '.join(sorted(unknown))}")
        return cls(**data)


@dataclass
class Config:
    sites_file: Path
    state_db: Path
    key_file: Path
    crawl: CrawlSettings
    storage: dict = field(default_factory=dict)


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
        crawl=CrawlSettings.from_dict(data.get("crawl", {})),
        storage=storage,
    )
