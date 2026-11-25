"""Property ids as text we draw ourselves, for the "Property ids" layer.

The Land Survey's ready-made id tiles are pictures: the text is blurry when zoomed past
their one zoom level, of many sizes, and cut at tile edges. Its open cadastral data also
gives each id as a point (OGC API Features, with the same API key as the maps), so the
panel asks for those per zoom-15 tile and writes the ids itself: sharp, one size, never
cut, and tappable. Kept gzipped on disk for a month.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
from pathlib import Path

import aiohttp

from .config import USER_AGENT
from .mapsources import tile_box

_LOGGER = logging.getLogger(__name__)

API = "https://avoin-paikkatieto.maanmittauslaitos.fi/kiinteisto-avoin/simple-features/v3"
COLLECTION = "KiinteistotunnuksenSijaintitiedot"
TILE_Z = 15
MAX_AGE = 30 * 86400
PAGES = 4
ID_RE = re.compile(r"^\d{1,3}-\d{1,3}-\d{1,4}-\d{1,4}(?:-[A-Z]\d+)?$")
BOX = (59.3, 19.0, 70.2, 31.7)


def pretty_id(raw: str) -> str | None:
    """'25747000030028' -> '257-470-3-28'; an id already written with dashes is kept."""
    raw = str(raw or "").strip()
    if ID_RE.match(raw):
        return raw
    if re.fullmatch(r"\d{14}", raw):
        return "-".join(str(int(part)) for part in (raw[:3], raw[3:6], raw[6:10], raw[10:]))
    return None


def compact(collection: dict) -> list[list]:
    """GeoJSON points -> rows [lat, lon, id], each id once per place."""
    rows, seen = [], set()
    for f in collection.get("features") or []:
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates")
        try:
            if geom.get("type") == "Point":
                lon, lat = coords[0], coords[1]
            elif geom.get("type") == "MultiPoint":
                lon, lat = coords[0][0], coords[0][1]
            else:
                continue
        except (TypeError, IndexError):
            continue
        p = f.get("properties") or {}
        text = None
        for key in ("kiinteistotunnuksenEsitysmuoto", "tekstiKartalla", "kiinteistotunnus"):
            text = pretty_id(p.get(key))
            if text:
                break
        if not text:  # field names we don't know: any value that reads as an id
            text = next((t for t in (pretty_id(v) for v in p.values() if isinstance(v, str)) if t), None)
        key = (text, round(lat, 5), round(lon, 5))
        if text and key not in seen:
            seen.add(key)
            rows.append([round(lat, 6), round(lon, 6), text])
    return rows


class Parcels:
    def __init__(self, directory: Path, session: aiohttp.ClientSession, store, fake: bool = False):
        self.dir = directory
        self.session = session
        self.store = store
        self.fake = fake
        self.failing: str | None = None
        self._limit = asyncio.Semaphore(2)

    def _path(self, x: int, y: int) -> Path:
        return self.dir / str(x) / f"{y}.json.gz"

    async def tile(self, x: int, y: int) -> bytes | None:
        s, w, n, e = tile_box(TILE_Z, x, y)
        if not (s < BOX[2] and n > BOX[0] and w < BOX[3] and e > BOX[1]):
            return None
        path = self._path(x, y)
        try:
            if time.time() - path.stat().st_mtime < MAX_AGE:
                return path.read_bytes()
        except FileNotFoundError:
            pass
        try:
            rows = await self._fetch(s, w, n, e)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            self.failing = str(err) or type(err).__name__
            _LOGGER.warning("Property ids: tile %s/%s failed: %s", x, y, self.failing)
            return path.read_bytes() if path.exists() else None
        self.failing = None
        body = gzip.compress(json.dumps({"rows": rows}, separators=(",", ":")).encode(), 6)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)
        return body

    async def _fetch(self, s: float, w: float, n: float, e: float) -> list[list]:
        if self.fake:
            import random
            rnd = random.Random(int(s * 1e4) * 31 + int(w * 1e4))
            return [[round(rnd.uniform(s, n), 6), round(rnd.uniform(w, e), 6), f"257-470-{rnd.randrange(1, 9)}-{rnd.randrange(1, 300)}"]
                    for _ in range(40)]
        key = self.store.kv_get("mml_api_key", "")
        if not key:
            raise ValueError("no Land Survey API key")
        url: str | None = f"{API}/collections/{COLLECTION}/items"
        params: dict | None = {"bbox": f"{w:.6f},{s:.6f},{e:.6f},{n:.6f}", "limit": "1000", "api-key": key}
        rows: list[list] = []
        for _ in range(PAGES):
            async with self._limit, self.session.get(url, params=params, headers={"User-Agent": USER_AGENT},
                                                     timeout=aiohttp.ClientTimeout(total=45)) as resp:
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}")
                data = await resp.json(content_type=None)
            rows += compact(data)
            url = next((l.get("href") for l in data.get("links") or [] if l.get("rel") == "next"), None)
            if not url:
                break
            params = None if "api-key" in url else {"api-key": key}
        return rows

    def prune(self) -> int:
        removed, now = 0, time.time()
        for p in self.dir.glob("*/*.json.gz"):
            try:
                if now - p.stat().st_mtime > 3 * MAX_AGE:
                    p.unlink()
                    removed += 1
            except FileNotFoundError:
                pass
        return removed
