"""Keeps a copy of nautical chart and sea mark tiles on disk.

Traficom's chart server and OpenSeaMap are slow, so the panel asks the add-on
for these tiles instead: the first view of an area fetches them, later views
(on any device) come straight from the NAS. Tiles are refreshed after a month,
and the oldest are removed once the cache passes its size limit.

The weather overlays use a cache of their own (its own folder and size limit),
where each source can have a shorter life: a radar frame is fetched again, or
removed from the disk, after hours rather than a month.
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
    def __init__(self, directory: Path, session: aiohttp.ClientSession, max_bytes: int = MAX_BYTES):
        self.dir = directory
        self.session = session
        self.max_bytes = max_bytes
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
        return self.max_ages.get(_top(source), MAX_AGE)

    async def get(self, source: str, z: int, x: int, y: int, url: str) -> Path | None:
        """Path to the tile on disk, fetching it first if missing or old. None if the
        tile doesn't exist upstream (sea marks have no tile where there are no marks)."""
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
            result = await self._fetch(source, path, url)
        except BaseException:
            future.cancel()
            raise
        else:
            future.set_result(result)
            return result
        finally:
            del self._pending[path]

    async def _fetch(self, source: str, path: Path, url: str) -> Path | None:
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
        self.failing.pop(_top(source), None)
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
        chart tile is still shown when the chart server is down."""
        now = now or time.time()
        files = []
        total = 0
        removed = 0
        for root, _, names in os.walk(self.dir):
            rel = Path(root).relative_to(self.dir).parts
            limit = self.max_ages.get(rel[0]) if rel else None
            for name in names:
                p = Path(root) / name
                try:
                    st = p.stat()
                except FileNotFoundError:
                    continue
                if limit is not None and now - st.st_mtime >= limit:
                    p.unlink(missing_ok=True)
                    removed += 1
                    continue
                files.append((st.st_mtime, st.st_size, p))
                total += st.st_size
        for _, size, p in sorted(files):
            if total <= self.max_bytes:
                break
            p.unlink(missing_ok=True)
            total -= size
            removed += 1
        # Each radar frame leaves a folder tree behind; empty ones go too (rmdir refuses the rest).
        for top in self.max_ages:
            for root, _, _ in os.walk(self.dir / top, topdown=False):
                try:
                    os.rmdir(root)
                except OSError:
                    pass
        return removed
