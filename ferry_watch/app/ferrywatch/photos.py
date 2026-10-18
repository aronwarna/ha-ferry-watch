"""Ship photos: the lead photo of each ship's Wikipedia article, cached locally.

Wikipedia lead photos are picked by editors and are freely licensed
(Wikimedia Commons), which makes them a good default. Users can upload their
own photo per ship, which is never overwritten.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp

from .config import USER_AGENT

_LOGGER = logging.getLogger(__name__)

WIKI_API = "https://en.wikipedia.org/w/api.php"
THUMB_WIDTH = 640
RETRY_AFTER = 6 * 3600
MAX_UPLOAD = 8 * 1024 * 1024
SHIP_WORDS = ("ship", "ferry", "vessel", "cruise", "liner", "boat")
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class PhotoCache:
    def __init__(self, directory: Path, session: aiohttp.ClientSession):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.session = session
        self._locks: dict[int, asyncio.Lock] = {}

    def _info_path(self, mmsi: int) -> Path:
        return self.dir / f"{mmsi}.json"

    def info(self, mmsi: int) -> dict[str, Any]:
        try:
            return json.loads(self._info_path(mmsi).read_text())
        except (OSError, ValueError):
            return {}

    def _write_info(self, mmsi: int, info: dict[str, Any]) -> None:
        self._info_path(mmsi).write_text(json.dumps(info))

    def path(self, mmsi: int) -> Path | None:
        name = self.info(mmsi).get("file")
        if not name:
            return None
        path = self.dir / name
        return path if path.exists() else None

    def remote_url(self, mmsi: int) -> str | None:
        return self.info(mmsi).get("remote_url")

    def public_info(self, mmsi: int) -> dict[str, Any] | None:
        info = self.info(mmsi)
        if not self.path(mmsi):
            return None
        return {
            "version": info.get("fetched_ts", 0),
            "uploaded": info.get("uploaded", False),
            "credit_url": info.get("page_url"),
        }

    async def ensure(self, ship: dict[str, Any]) -> None:
        mmsi = ship["mmsi"]
        lock = self._locks.setdefault(mmsi, asyncio.Lock())
        async with lock:
            info = self.info(mmsi)
            if self.path(mmsi) or time.time() - info.get("failed_ts", 0) < RETRY_AFTER:
                return
            titles = [ship["wiki"]] if ship.get("wiki") else [f"MS {ship['name']}", ship["name"]]
            for title in titles:
                try:
                    if await self._fetch_from_wikipedia(mmsi, title):
                        return
                except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                    _LOGGER.debug("Photo lookup for %s failed: %s", title, err)
            self._write_info(mmsi, {"failed_ts": int(time.time())})

    async def _fetch_from_wikipedia(self, mmsi: int, title: str) -> bool:
        params = {
            "action": "query", "format": "json", "formatversion": "2", "redirects": "1",
            "prop": "pageimages|description", "piprop": "thumbnail", "pithumbsize": str(THUMB_WIDTH),
            "titles": title,
        }
        headers = {"User-Agent": USER_AGENT}
        timeout = aiohttp.ClientTimeout(total=30)
        async with self.session.get(WIKI_API, params=params, headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            data = await resp.json()
        pages = data.get("query", {}).get("pages", [])
        if not pages or "thumbnail" not in pages[0]:
            return False
        page = pages[0]
        description = (page.get("description") or "").lower()
        if not any(word in description for word in SHIP_WORDS):
            return False  # some other article with the same name
        url = page["thumbnail"]["source"]
        async with self.session.get(url, headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            ext = EXTENSIONS.get(resp.content_type, ".jpg")
            body = await resp.read()
        self._save(mmsi, body, ext, {
            "remote_url": url,
            "page_url": f"https://en.wikipedia.org/wiki/{page['title'].replace(' ', '_')}",
            "uploaded": False,
        })
        _LOGGER.info("Fetched photo for %s from %s", mmsi, page["title"])
        return True

    def save_upload(self, mmsi: int, body: bytes, content_type: str) -> None:
        if content_type not in EXTENSIONS:
            raise ValueError("Photo must be JPEG, PNG or WebP")
        if len(body) > MAX_UPLOAD:
            raise ValueError("Photo is larger than 8 MB")
        self._save(mmsi, body, EXTENSIONS[content_type], {"uploaded": True})

    def _save(self, mmsi: int, body: bytes, ext: str, info: dict[str, Any]) -> None:
        old = self.path(mmsi)
        name = f"{mmsi}{ext}"
        (self.dir / name).write_bytes(body)
        if old and old.name != name:
            old.unlink(missing_ok=True)
        self._write_info(mmsi, {**info, "file": name, "fetched_ts": int(time.time())})
