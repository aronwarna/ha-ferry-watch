"""When each building was built: the Helsinki region's building register (HSY,
CC BY 4.0, Helsinki, Espoo, Vantaa and Kauniainen), for the "Building ages" layer.

The panel asks for one zoom-14 tile at a time; the NAS asks HSY's open WFS for the
buildings in it, keeps only what the layer shows (place, year, use, floors, address,
facade and frame material) and stores that gzipped on disk for a month. A tile is
about 1-20 kB that way, so RAM use stays tiny.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from pathlib import Path

import aiohttp

from .config import USER_AGENT
from .mapsources import tile_box

_LOGGER = logging.getLogger(__name__)

WFS = "https://kartta.hsy.fi/geoserver/wfs"
LAYER = "asuminen_ja_maankaytto:pks_rakennukset_paivittyva"
FIELDS = ("kavu", "katu", "osno1", "oski1", "kayttarks", "kerrosten_lkm", "julkisivu_s", "rakennusaine_s")
TILE_Z = 14
BOX = (60.06, 24.47, 60.42, 25.27)  # south, west, north, east: Helsinki, Espoo, Vantaa, Kauniainen
MAX_AGE = 30 * 86400
ATTRIBUTION = "Building register © HSY (CC BY 4.0)"


def _centre(geom: dict) -> tuple[float, float] | None:
    """(lat, lon) of a point, or the middle of a polygon's outer ring."""
    if not geom:
        return None
    kind, coords = geom.get("type"), geom.get("coordinates")
    try:
        if kind == "Point":
            return coords[1], coords[0]
        if kind == "MultiPoint":
            return coords[0][1], coords[0][0]
        ring = coords[0] if kind == "Polygon" else coords[0][0] if kind == "MultiPolygon" else None
        if not ring:
            return None
        return sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring)
    except (TypeError, IndexError, ZeroDivisionError):
        return None


def compact(collection: dict) -> list[list]:
    """GeoJSON from the WFS -> rows [lat, lon, year, use, floors, address, facade, frame]."""
    rows = []
    for f in collection.get("features") or []:
        at = _centre(f.get("geometry"))
        if not at:
            continue
        p = f.get("properties") or {}
        year = p.get("kavu")
        try:
            year = int(year) if year and 1000 < float(year) < 2100 else None
        except (TypeError, ValueError):
            year = None
        street = " ".join(str(v) for v in (p.get("katu"), p.get("osno1") and int(p["osno1"]), p.get("oski1")) if v)
        floors = p.get("kerrosten_lkm")
        rows.append([round(at[0], 6), round(at[1], 6), year, p.get("kayttarks") or "",
                     int(floors) if isinstance(floors, (int, float)) and floors > 0 else None,
                     street, p.get("julkisivu_s") or "", p.get("rakennusaine_s") or ""])
    return rows


def covers(x: int, y: int) -> bool:
    s, w, n, e = tile_box(TILE_Z, x, y)
    return s < BOX[2] and n > BOX[0] and w < BOX[3] and e > BOX[1]


class Buildings:
    def __init__(self, directory: Path, session: aiohttp.ClientSession, fake: bool = False):
        self.dir = directory
        self.session = session
        self.fake = fake
        self.failing: str | None = None
        self._limit = asyncio.Semaphore(3)

    def _path(self, x: int, y: int) -> Path:
        return self.dir / str(x) / f"{y}.json.gz"

    async def tile(self, x: int, y: int) -> bytes | None:
        """Gzipped JSON {"rows": [...]} for a zoom-14 tile, or None outside the region."""
        if not covers(x, y):
            return None
        path = self._path(x, y)
        try:
            if time.time() - path.stat().st_mtime < MAX_AGE:
                return path.read_bytes()
        except FileNotFoundError:
            pass
        try:
            rows = await self._fetch(x, y)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            self.failing = str(err) or type(err).__name__
            _LOGGER.warning("Building ages: tile %s/%s failed: %s", x, y, self.failing)
            return path.read_bytes() if path.exists() else None
        self.failing = None
        body = gzip.compress(json.dumps({"rows": rows}, separators=(",", ":")).encode(), 6)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)
        return body

    async def _fetch(self, x: int, y: int) -> list[list]:
        s, w, n, e = tile_box(TILE_Z, x, y)
        if self.fake:
            from .simmaps import fake_buildings
            return compact(fake_buildings(s, w, n, e))
        params = {
            "service": "WFS", "version": "1.0.0", "request": "GetFeature", "typeName": LAYER,
            "outputFormat": "application/json", "srsName": "EPSG:4326",
            "propertyName": ",".join(FIELDS + ("geom",)),
            # WFS 1.0.0 bbox is lon,lat order for EPSG:4326.
            "bbox": f"{w:.6f},{s:.6f},{e:.6f},{n:.6f},EPSG:4326", "maxFeatures": "6000",
        }
        async with self._limit, self.session.get(WFS, params=params, headers={"User-Agent": USER_AGENT},
                                                 timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        return compact(data)

    def prune(self) -> int:
        removed = 0
        now = time.time()
        for p in self.dir.glob("*/*.json.gz"):
            try:
                if now - p.stat().st_mtime > 3 * MAX_AGE:
                    p.unlink()
                    removed += 1
            except FileNotFoundError:
                pass
        return removed
