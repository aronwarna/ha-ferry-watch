"""Keeps a copy of nautical chart and sea mark tiles on disk.

Traficom's chart server and OpenSeaMap are slow, so the panel asks the add-on
for these tiles instead: the first view of an area fetches them, later views
(on any device) come straight from the NAS. Tiles are refreshed after a month,
and the oldest are removed once the cache passes its size limit.

The weather overlays use a cache of their own (its own folder and size limit),
where each source can have a shorter life: a radar frame is fetched again, or
removed from the disk, after hours rather than a month.

A copy on disk is only ever replaced by a better one: when a refresh fails, or the
service now answers "nothing here" or something unreadable, the old tile stays (and is
asked for again only after another max age). So a service that goes away, or breaks,
never takes away what was already seen. A cache made with ``delete_stale=False`` (old
photos and maps) never removes tiles for age at all, only the oldest when it's full.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import aiohttp

from .config import USER_AGENT

_LOGGER = logging.getLogger(__name__)

MAX_AGE = 30 * 86400
MAX_BYTES = 300 * 1024 * 1024
CONCURRENCY = 6


def _top(source: str) -> str:
    """The folder a source's tiles live under: "radar/1761400800" -> "radar"."""
    return source.split("/", 1)[0]


class TileCache:
    def __init__(self, directory: Path, session: aiohttp.ClientSession, max_bytes: int = MAX_BYTES,
                 delete_stale: bool = True):
        self.dir = directory
        self.delete_stale = delete_stale
        self.default_age = MAX_AGE  # refresh age of sources not in max_ages (never deleted for age)
        self.session = session
        self.max_bytes = max_bytes
        self.size_seen: tuple[float, int] = (0.0, 0)  # (when, bytes) from the last prune
        # Sources with a shorter life than MAX_AGE (by their top folder). Their tiles are
        # fetched again when older, and prune() removes them from the disk.
        self.max_ages: dict[str, float] = {}
        # Sources whose last fetch failed, with the error; cleared by the next success.
        self.failing: dict[str, str] = {}
        self._limit = asyncio.Semaphore(CONCURRENCY)
        self._pending: dict[Path, asyncio.Future] = {}

    def path(self, source: str, z: int, x: int, y: int) -> Path:
        return self.dir / source / str(z) / str(x) / f"{y}.png"

    def max_age(self, source: str) -> float:
        return self.max_ages.get(_top(source), self.default_age)

    async def get(self, source: str, z: int, x: int, y: int, url: str, transform=None) -> Path | None:
        """Path to the tile on disk, fetching it first if missing or old. None if the
        tile doesn't exist upstream (sea marks have no tile where there are no marks).
        ``transform`` (bytes -> bytes, run in a thread) changes a fetched tile before it's
        kept; b"" from it means "nothing here"."""
        path = self.path(source, z, x, y)
        try:
            if time.time() - path.stat().st_mtime < self.max_age(source):
                return path if path.stat().st_size else None
        except FileNotFoundError:
            pass
        # Several map views can ask for the same tile at once; fetch it only once.
        if path in self._pending:
            return await asyncio.shield(self._pending[path])
        future = asyncio.get_running_loop().create_future()
        self._pending[path] = future
        try:
            result = await self._fetch(source, path, url, transform)
        except BaseException:
            future.cancel()
            raise
        else:
            future.set_result(result)
            return result
        finally:
            del self._pending[path]

    @staticmethod
    def _keep_old(path: Path) -> Path | None:
        """The copy already on disk, if it's a real picture: marked as checked now, so it's
        asked for again only after another max age."""
        try:
            if path.stat().st_size:
                os.utime(path)
                return path
        except FileNotFoundError:
            pass
        return None

    async def _fetch(self, source: str, path: Path, url: str, transform=None) -> Path | None:
        try:
            async with self._limit, self.session.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status in (400, 404):
                    body = b""  # outside the chart: remembered so it isn't asked for again
                else:
                    resp.raise_for_status()
                    if not resp.content_type.startswith("image/"):
                        raise aiohttp.ClientError(f"not an image: {resp.content_type}")  # e.g. a WMTS error
                    body = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Tile %s failed: %s", url, err)
            self.failing[_top(source)] = str(err) or type(err).__name__
            return path if path.exists() and path.stat().st_size else None  # an old copy beats none
        if transform is not None and body:
            try:
                body = await asyncio.to_thread(transform, body)
            except Exception as err:  # noqa: BLE001 - a picture we can't read isn't kept
                _LOGGER.debug("Tile %s couldn't be read: %s", url, err)
                self.failing[_top(source)] = f"unreadable picture: {err}"
                return self._keep_old(path)
        self.failing.pop(_top(source), None)
        if not body:
            old = self._keep_old(path)
            if old is not None:
                return old  # "nothing here" now, a picture before: the picture stays
        tmp = path.with_suffix(".tmp")
        for _ in range(2):  # twice: prune() may remove the folder between the two steps
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                tmp.write_bytes(body)
                os.replace(tmp, path)
                break
            except FileNotFoundError:
                continue
        return path if body else None

    def prune(self, now: float | None = None) -> int:
        """Remove tiles past their source's own max age, then the least recently fetched
        until the cache fits, then folders left empty; returns how many tiles went.
        Tiles of sources without a max age of their own are kept when old: a month-old
        chart tile is still shown when the chart server is down. Memory stays small however
        many tiles there are (a kept store can hold millions): the first walk only adds up
        bytes per minute of last fetch, the second removes the oldest minutes."""
        now = now or time.time()
        minutes: dict[int, int] = {}
        total = 0
        removed = 0
        for root, _, names in os.walk(self.dir):
            rel = Path(root).relative_to(self.dir).parts
            limit = self.max_ages.get(rel[0]) if rel and self.delete_stale else None
            for name in names:
                p = os.path.join(root, name)
                try:
                    st = os.stat(p)
                except FileNotFoundError:
                    continue
                if limit is not None and now - st.st_mtime >= limit:
                    _unlink(p)
                    removed += 1
                    continue
                h = int(st.st_mtime // 60)
                minutes[h] = minutes.get(h, 0) + st.st_size
                total += st.st_size
        if total > self.max_bytes:
            cutoff, left = None, total
            for h in sorted(minutes):
                if left <= self.max_bytes:
                    break
                left -= minutes[h]
                cutoff = h
            for root, _, names in os.walk(self.dir):
                for name in names:
                    p = os.path.join(root, name)
                    try:
                        st = os.stat(p)
                    except FileNotFoundError:
                        continue
                    if int(st.st_mtime // 60) <= cutoff:
                        _unlink(p)
                        total -= st.st_size
                        removed += 1
        self.size_seen = (time.time(), total)
        # Each radar frame leaves a folder tree behind; empty ones go too (rmdir refuses the rest).
        for top in self.max_ages if self.delete_stale else ():
            for root, _, _ in os.walk(self.dir / top, topdown=False):
                try:
                    os.rmdir(root)
                except OSError:
                    pass
        return removed


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
