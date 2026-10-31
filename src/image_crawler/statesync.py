"""Keep the crawl history and the site list in the storage too, encrypted (`state_sync = true`).

A cloud runner starts from an empty disk every time. Without its state_db it would
download every image it has already seen again (R2 still dedupes the uploads) and,
worse, start the R2 quota meter from zero. With state_sync each command:

1. pull(), before the database is opened: fetches state/crawl-state.bin and, if it is
   newer than the local copy, replaces the local state_db and sites file with it;
2. push(), when the command is done (even if it failed): if it changed the database or
   the site list, uploads a new snapshot.

Every snapshot carries a generation number, also kept in the local database. pull()
only installs a snapshot whose generation is higher than the local one, so a stale
state.db (say, an old copy on a PC) never replaces newer history, and push() refuses to
overwrite a snapshot that someone else uploaded after this command pulled. Runs are
meant to be sequential (one scheduled runner); if two overlap, the later one keeps its
history locally instead of clobbering the other's.

The snapshot is a SQLite backup of state_db plus the sites file, zlib-compressed and
sealed like every other blob (AES-GCM, bound to its slot), so the provider still only
sees an opaque, padded blob.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from image_crawler import layout
from image_crawler.crypto import KIND_STATE, SMALL_PAD_BLOCK, DecryptionError, Vault
from image_crawler.state import State
from image_crawler.storage import Storage

log = logging.getLogger(__name__)

FORMAT = 1
SLOT = "crawl-state"
GENERATION = "sync_generation"


@dataclass
class Snapshot:
    generation: int
    saved_at: str
    sites: str | None
    database: bytes
    blob_size: int


def read_snapshot(vault: Vault, storage: Storage) -> Snapshot | None:
    """The snapshot in the storage, or None if there is none yet."""
    if not storage.exists(layout.STATE_KEY):
        return None
    blob = storage.get(layout.STATE_KEY)
    try:
        opened = vault.open(blob, kind=KIND_STATE, name=SLOT)
    except DecryptionError as e:
        raise RuntimeError(
            f"cannot decrypt the crawl state backup ({layout.STATE_KEY}): {e}. "
            "Is this the key the crawler used?"
        ) from e
    meta = opened.metadata
    if meta.get("v") != FORMAT:
        raise RuntimeError(f"unsupported crawl state backup format: {meta.get('v')!r}")
    return Snapshot(
        generation=int(meta["generation"]),
        saved_at=meta.get("saved_at", ""),
        sites=meta.get("sites"),
        database=zlib.decompress(opened.content),
        blob_size=len(blob),
    )


def local_generation(path: Path) -> int:
    """Generation of the local database; 0 if it is missing or has never been synced."""
    if not path.exists():
        return 0
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (GENERATION,)).fetchone()
    except sqlite3.OperationalError:  # created before the meta table existed
        row = None
    finally:
        db.close()
    return int(row[0]) if row else 0


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".sync-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _database_bytes(state: State) -> bytes:
    """A consistent copy of the open database."""
    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / "state.db"
        copy = sqlite3.connect(copy_path)
        try:
            state.db.backup(copy)
        finally:
            copy.close()
        return copy_path.read_bytes()


class StateSync:
    def __init__(self, vault: Vault, storage: Storage, state_db: Path, sites_file: Path):
        self.vault = vault
        self.storage = storage  # for pull(): the database that meters requests isn't open yet
        self.state_db = state_db.expanduser()
        self.sites_file = sites_file.expanduser()
        self.pull_requests = 0  # Class B requests pull() made, for the caller to meter
        self.base_generation = 0  # what this command's database descends from
        self.remote_generation = 0  # what the storage held when pull() looked
        self._sites_before: str | None = None
        self._changes_before = 0

    # --- start of a command ---------------------------------------------

    def pull(self) -> str:
        """Install the stored snapshot if it is newer than the local state. Returns a status line."""
        local = local_generation(self.state_db)
        snapshot = read_snapshot(self.vault, self.storage)
        self.pull_requests = 1 if snapshot is None else 2
        self.remote_generation = snapshot.generation if snapshot else 0
        self.base_generation = max(local, self.remote_generation)

        if snapshot is None:
            status = "no crawl state backup yet; starting from the local state"
        elif snapshot.generation > local:
            self._install(snapshot)
            status = f"restored crawl state generation {snapshot.generation} ({snapshot.saved_at})"
        elif snapshot.generation < local:
            status = (
                f"local crawl state (generation {local}) is newer than the backup "
                f"(generation {snapshot.generation}); keeping the local one"
            )
            log.warning(status)
        else:
            status = f"crawl state is up to date (generation {local})"
        log.info(status)
        self._sites_before = self._read_sites()
        return status

    def _install(self, snapshot: Snapshot) -> None:
        if self.state_db.exists():
            os.replace(self.state_db, self.state_db.with_name(self.state_db.name + ".bak"))
        _write_atomic(self.state_db, snapshot.database)
        if snapshot.sites is not None:
            _write_atomic(self.sites_file, snapshot.sites.encode())

    def begin(self, state: State) -> None:
        """Remember the database's change counter once it is open (and pull requests are metered)."""
        self._changes_before = state.db.total_changes

    # --- end of a command -------------------------------------------------

    def changed(self, state: State) -> bool:
        return state.db.total_changes != self._changes_before or self._read_sites() != self._sites_before

    def push(self, state: State, storage: Storage) -> int | None:
        """Upload a new snapshot if this command changed anything. Returns its generation.

        `storage` should be the metered (quota-guarded) storage. Raises RuntimeError
        if someone else uploaded a snapshot after pull(); the local state is kept.
        """
        if not self.changed(state):
            return None
        current = read_snapshot(self.vault, storage)
        current_generation = current.generation if current else 0
        if current_generation > self.remote_generation:
            raise RuntimeError(
                f"the crawl state backup changed while this command ran (generation "
                f"{self.remote_generation} -> {current_generation}, another run?); not overwriting "
                f"it. This machine's history stays in {self.state_db}"
            )
        generation = max(self.base_generation, current_generation) + 1
        state.set_meta(GENERATION, str(generation))
        blob = self._seal(state, generation)
        # The quota meter records the blob's size only after upload, i.e. after the snapshot.
        # Record it first so the next machine's storage total includes the backup itself.
        target = getattr(storage, "target", None)  # set on the quota-guarded storage
        for _ in range(3):
            if target is None or state.stored_size(target, layout.STATE_KEY) == len(blob):
                break
            state.record_stored(target, layout.STATE_KEY, len(blob))
            blob = self._seal(state, generation)  # padding makes a size change here unlikely
        storage.put(layout.STATE_KEY, blob)
        self.base_generation = self.remote_generation = generation
        self._changes_before = state.db.total_changes
        self._sites_before = self._read_sites()
        return generation

    def _seal(self, state: State, generation: int) -> bytes:
        return self.vault.seal(
            zlib.compress(_database_bytes(state), 9),
            {
                "v": FORMAT,
                "generation": generation,
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "sites": self._read_sites(),
            },
            kind=KIND_STATE,
            name=SLOT,
            pad_block=SMALL_PAD_BLOCK,
        )

    def _read_sites(self) -> str | None:
        try:
            return self.sites_file.read_text()
        except FileNotFoundError:
            return None


def describe_backup(vault: Vault, storage: Storage) -> str:
    """One line about the stored snapshot, for `image-crawler check`."""
    snapshot = read_snapshot(vault, storage)
    if snapshot is None:
        return "none yet (created after the first command that changes the crawl state)"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.db"
        path.write_bytes(snapshot.database)
        db = sqlite3.connect(path)
        try:
            images = db.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        finally:
            db.close()
    sites = len([s for s in (snapshot.sites or "").splitlines() if s.strip() and not s.startswith("#")])
    return (
        f"generation {snapshot.generation}, saved {snapshot.saved_at}, {images} image URL(s), "
        f"{sites} site(s), {snapshot.blob_size / 1000:.0f} KB"
    )
