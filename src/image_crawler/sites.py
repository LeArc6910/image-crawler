"""The site list: a plain text file with one URL per line (`#` starts a comment)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit


def normalize_site(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"not an http(s) URL: {url}")
    return url


def read_sites(path: Path) -> list[str]:
    if not path.exists():
        return []
    sites = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            sites.append(normalize_site(line))
    return list(dict.fromkeys(sites))


def add_sites(path: Path, urls: list[str]) -> list[str]:
    """Append URLs that are not listed yet; returns the ones actually added."""
    existing = set(read_sites(path))
    added = [u for u in dict.fromkeys(normalize_site(u) for u in urls) if u not in existing]
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = ""
        if path.exists() and path.read_text() and not path.read_text().endswith("\n"):
            prefix = "\n"
        with path.open("a") as f:
            f.write(prefix + "".join(u + "\n" for u in added))
    return added


def remove_sites(path: Path, urls: list[str]) -> list[str]:
    """Drop matching lines (comments and other lines are kept); returns the removed URLs."""
    if not path.exists():
        return []
    targets = {normalize_site(u) for u in urls}
    kept, removed = [], []
    for line in path.read_text().splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry and normalize_site(entry) in targets:
            removed.append(normalize_site(entry))
        else:
            kept.append(line)
    path.write_text("".join(line + "\n" for line in kept))
    return removed
