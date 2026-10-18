"""Keeps a copy of nautical chart and sea mark tiles on disk.

Traficom's chart server and OpenSeaMap are slow, so the panel asks the add-on
for these tiles instead: the first view of an area fetches them, later views
(on any device) come straight from the NAS. Tiles are refreshed after a month,
and the oldest are removed once the cache passes its size limit.
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


class TileCache:
    def __init__(self, directory: Path, session: aiohttp.ClientSession, max_bytes: int = MAX_BYTES):
        self.dir = directory
        self.session = session
        self.max_bytes = max_bytes
        self._limit = asyncio.Semaphore(CONCURRENCY)
        self._pending: dict[Path, asyncio.Future] = {}

    def path(self, source: str, z: int, x: int, y: int) -> Path:
        return self.dir / source / str(z) / str(x) / f"{y}.png"

    async def get(self, source: str, z: int, x: int, y: int, url: str) -> Path | None:
        """Path to the tile on disk, fetching it first if missing or old. None if the
        tile doesn't exist upstream (sea marks have no tile where there are no marks)."""
        path = self.path(source, z, x, y)
        try:
            if time.time() - path.stat().st_mtime < MAX_AGE:
                return path if path.stat().st_size else None
        except FileNotFoundError:
            pass
        # Several map views can ask for the same tile at once; fetch it only once.
        if path in self._pending:
            return await asyncio.shield(self._pending[path])
        future = asyncio.get_running_loop().create_future()
        self._pending[path] = future
        try:
            result = await self._fetch(path, url)
        except BaseException:
            future.cancel()
            raise
        else:
            future.set_result(result)
            return result
        finally:
            del self._pending[path]

    async def _fetch(self, path: Path, url: str) -> Path | None:
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
            return path if path.exists() and path.stat().st_size else None  # an old copy beats none
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, path)
        return path if body else None

    def prune(self) -> int:
        """Remove the least recently fetched tiles until the cache fits; returns how many."""
        files = []
        total = 0
        for root, _, names in os.walk(self.dir):
            for name in names:
                p = Path(root) / name
                try:
                    st = p.stat()
                except FileNotFoundError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
                total += st.st_size
        removed = 0
        for _, size, p in sorted(files):
            if total <= self.max_bytes:
                break
            p.unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed
