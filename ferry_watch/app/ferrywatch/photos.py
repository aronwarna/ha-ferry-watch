"""Ship photos, cached locally, all from Wikimedia (free licences, no key, no scraping):

1. the lead photo of the ship's Wikipedia article (picked by editors), for my ships and
   fleet ships, whose names are known to be right;
2. Wikimedia Commons' "IMO nnnnnnn" category, which ship photographers fill for a great
   many merchant ships, ferries and tugs; the IMO number makes it the exact ship;
3. the Wikidata item with that IMO number (property P458) and its image (P18).

Users can upload their own photo per ship, which is never overwritten. Photos looked up
for "other ships" go to a second cache that keeps only the newest MAX_OTHERS files.
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

WIKI_API = "https://{lang}.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp")
# Bumped when new sources are added, so ships that found nothing before are tried again.
LOOKUP_REV = 3
THUMB_WIDTH = 640
RETRY_AFTER = 7 * 24 * 3600
MAX_UPLOAD = 8 * 1024 * 1024
# English, Finnish and Swedish words that show an article is about a ship.
SHIP_WORDS = ("ship", "ferry", "vessel", "cruise", "liner", "boat", "icebreaker",
              "alus", "lautta", "laiva", "murtaja", "fartyg", "färja", "båt")
SHIP_PREFIXES = ("MS ", "M/S ", "MV ", "L/A ", "M/V ")
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class PhotoCache:
    def __init__(self, directory: Path, session: aiohttp.ClientSession | None, max_files: int = 0):
        self.dir = directory
        self.max_files = max_files  # 0: keep every photo
        self.dir.mkdir(parents=True, exist_ok=True)
        self.session = session
        self._locks: dict[int, asyncio.Lock] = {}
        self._warned = 0.0

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
            "source": info.get("source") or ("Wikipedia" if info.get("page_url") else None),
        }

    def tried(self, mmsi: int) -> bool:
        """Whether a lookup already found nothing lately (so asking again would be wasted)."""
        info = self.info(mmsi)
        return info.get("rev") == LOOKUP_REV and time.time() - info.get("failed_ts", 0) < RETRY_AFTER

    @staticmethod
    def candidates(ship: dict[str, Any]) -> list[tuple[str, str]]:
        """(wiki language, article title) pairs to try, best first."""
        if ship.get("wiki"):
            return [("en", ship["wiki"])]
        name = ship.get("name")
        if not name or ship.get("by_name") is False:
            return []
        return [("en", f"MS {name}"), ("en", f"MV {name}"), ("en", name),
                ("fi", f"M/S {name}"), ("fi", f"L/A {name}"), ("sv", f"M/S {name}")]

    async def ensure(self, ship: dict[str, Any]) -> bool:
        """Fetch a photo if the ship has none yet; returns whether one was fetched."""
        mmsi = ship["mmsi"]
        lock = self._locks.setdefault(mmsi, asyncio.Lock())
        async with lock:
            if self.path(mmsi) or self.tried(mmsi):
                return False
            imo = valid_imo(ship.get("imo"))
            by_name: list[tuple[str, Any]] = [
                (title, lambda lang=lang, title=title: self._fetch_from_wikipedia(mmsi, lang, title))
                for lang, title in self.candidates(ship)]
            by_imo: list[tuple[str, Any]] = [] if not imo or ship.get("wiki") else [
                (f"Commons IMO {imo}", lambda: self._fetch_from_commons(mmsi, imo)),
                (f"Wikidata IMO {imo}", lambda: self._fetch_from_wikidata(mmsi, imo)),
                (f"Commons search {imo}", lambda: self._search_commons(mmsi, imo))]
            # A ship known only by its AIS name may share it with a famous one: IMO first.
            lookups = by_imo + by_name if ship.get("imo_first") else by_name + by_imo
            error = None
            for label, lookup in lookups:
                try:
                    if await lookup():
                        self._trim()
                        return True
                except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError, TypeError, ValueError) as err:
                    error = f"{label}: {err or type(err).__name__}"
                    if time.time() - self._warned > 600:
                        self._warned = time.time()
                        _LOGGER.warning("Photo lookup for %s (%s) failed: %s", mmsi, label, err or type(err).__name__)
            # A network fault is retried after an hour, "nothing there" after a week.
            when = int(time.time()) - (RETRY_AFTER - 3600 if error else 0)
            why = "error" if error else ("none" if lookups else "no_imo")
            self._write_info(mmsi, {"failed_ts": when, "rev": LOOKUP_REV, "why": why, "error": error})
            return False

    def miss(self, mmsi: int) -> dict[str, Any]:
        """Why there is no photo: {"why": "none" | "no_imo" | "error", "error": text}."""
        info = self.info(mmsi)
        return {"why": info.get("why", "none"), "error": info.get("error")}

    async def _search_commons(self, mmsi: int, imo: int) -> bool:
        """Commons files whose description mentions the IMO number (many aren't categorised)."""
        data = await self._get_json(COMMONS_API, {
            "action": "query", "generator": "search", "gsrsearch": f'"{imo}"', "gsrnamespace": "6",
            "gsrlimit": "5", "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": str(THUMB_WIDTH)})
        pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 0))
        for page in pages:
            ii = (page.get("imageinfo") or [{}])[0]
            if ii.get("mime") in IMAGE_MIMES and (ii.get("thumburl") or ii.get("url")):
                return await self._download(mmsi, ii.get("thumburl") or ii["url"],
                                            ii.get("descriptionurl"), "Wikimedia Commons")
        return False

    async def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        headers = {"User-Agent": USER_AGENT}
        async with self.session.get(url, params={"format": "json", "formatversion": "2", **params},
                                    headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _fetch_from_commons(self, mmsi: int, imo: int) -> bool:
        """The first photo in Commons' "IMO nnnnnnn" category."""
        data = await self._get_json(COMMONS_API, {
            "action": "query", "generator": "categorymembers", "gcmtitle": f"Category:IMO {imo}",
            "gcmtype": "file", "gcmlimit": "20", "prop": "imageinfo", "iiprop": "url|mime",
            "iiurlwidth": str(THUMB_WIDTH)})
        pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 0))
        for page in pages:
            ii = (page.get("imageinfo") or [{}])[0]
            if ii.get("mime") in IMAGE_MIMES and (ii.get("thumburl") or ii.get("url")):
                return await self._download(mmsi, ii.get("thumburl") or ii["url"],
                                            ii.get("descriptionurl"), "Wikimedia Commons")
        return False

    async def _fetch_from_wikidata(self, mmsi: int, imo: int) -> bool:
        """The image (P18) of the Wikidata item whose IMO number (P458) is `imo`."""
        found = await self._get_json(WIKIDATA_API, {
            "action": "query", "list": "search", "srsearch": f"haswbstatement:P458={imo}", "srlimit": "1"})
        hits = found.get("query", {}).get("search", [])
        if not hits:
            return False
        qid = hits[0]["title"]
        claims = await self._get_json(WIKIDATA_API, {"action": "wbgetclaims", "entity": qid, "property": "P18"})
        files = [c["mainsnak"]["datavalue"]["value"] for c in claims.get("claims", {}).get("P18", [])
                 if c.get("mainsnak", {}).get("datavalue")]
        if not files:
            return False
        data = await self._get_json(COMMONS_API, {
            "action": "query", "titles": f"File:{files[0]}", "prop": "imageinfo", "iiprop": "url|mime",
            "iiurlwidth": str(THUMB_WIDTH)})
        page = (data.get("query", {}).get("pages") or [{}])[0]
        ii = (page.get("imageinfo") or [{}])[0]
        if ii.get("mime") not in IMAGE_MIMES:
            return False
        return await self._download(mmsi, ii.get("thumburl") or ii["url"],
                                    f"https://www.wikidata.org/wiki/{qid}", "Wikidata")

    async def _download(self, mmsi: int, url: str, page_url: str | None, source: str) -> bool:
        headers = {"User-Agent": USER_AGENT}
        async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            if resp.content_type not in EXTENSIONS:
                return False
            ext = EXTENSIONS[resp.content_type]
            body = await resp.content.read(MAX_UPLOAD + 1)
        if len(body) > MAX_UPLOAD:
            return False
        self._save(mmsi, body, ext, {"remote_url": url, "page_url": page_url, "source": source, "uploaded": False})
        _LOGGER.info("Fetched photo for %s from %s", mmsi, source)
        return True

    def _trim(self) -> None:
        """Keeps the newest `max_files` photos (and forgets old "found nothing" notes)."""
        if not self.max_files:
            return
        infos = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        kept = 0
        now = time.time()
        for path in infos:
            info = self.info(int(path.stem)) if path.stem.isdigit() else {}
            if info.get("file"):
                kept += 1
                if kept <= self.max_files:
                    continue
                (self.dir / info["file"]).unlink(missing_ok=True)
            elif now - path.stat().st_mtime < RETRY_AFTER:
                continue
            path.unlink(missing_ok=True)

    async def _fetch_from_wikipedia(self, mmsi: int, lang: str, title: str) -> bool:
        params = {
            "action": "query", "format": "json", "formatversion": "2", "redirects": "1",
            "prop": "pageimages|description", "piprop": "thumbnail", "pithumbsize": str(THUMB_WIDTH),
            "titles": title,
        }
        headers = {"User-Agent": USER_AGENT}
        timeout = aiohttp.ClientTimeout(total=30)
        async with self.session.get(WIKI_API.format(lang=lang), params=params, headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            data = await resp.json()
        pages = data.get("query", {}).get("pages", [])
        if not pages or "thumbnail" not in pages[0]:
            return False
        page = pages[0]
        description = (page.get("description") or "").lower()
        ship_title = page.get("title", "").startswith(SHIP_PREFIXES)
        if not any(word in description for word in SHIP_WORDS) and not (ship_title and not description):
            return False  # some other article with the same name
        url = page["thumbnail"]["source"]
        async with self.session.get(url, headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            ext = EXTENSIONS.get(resp.content_type, ".jpg")
            body = await resp.read()
        self._save(mmsi, body, ext, {
            "remote_url": url,
            "page_url": f"https://{lang}.wikipedia.org/wiki/{page['title'].replace(' ', '_')}",
            "source": "Wikipedia",
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


def valid_imo(value: Any) -> int | None:
    """An IMO number whose check digit is right, else None (AIS often carries 0 or junk)."""
    try:
        imo = int(value)
    except (TypeError, ValueError):
        return None
    if not 1_000_000 <= imo <= 9_999_999:
        return None
    digits = str(imo)
    check = sum(int(d) * w for d, w in zip(digits[:6], range(7, 1, -1))) % 10
    return imo if check == int(digits[6]) else None
