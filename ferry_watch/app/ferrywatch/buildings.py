"""When each building was built, for the "Building ages" layer: the Helsinki region's
building register (HSY, CC BY 4.0: Helsinki, Espoo, Vantaa, Kauniainen) and, everywhere
else in Finland, the national building register's open data (Ryhti, the Finnish
Environment Institute).

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


# The national register (Ryhti): open OGC API Features, no key. Collection and field names
# are read loosely, since the service is young and still changing.
RYHTI = "https://paikkatiedot.ymparisto.fi/geoserver/ryhti_building/ogc/features/v1"
RYHTI_COLLECTIONS = ("open_building", "open_completed_building", "open_buildings")
FINLAND = (59.3, 19.0, 70.2, 31.7)
RYHTI_ATTRIBUTION = "Buildings © Ryhti, Finnish Environment Institute"


def in_hsy(x: int, y: int) -> bool:
    s, w, n, e = tile_box(TILE_Z, x, y)
    # Wholly inside: a tile on the region's edge is asked from the national register.
    return s >= BOX[0] and n <= BOX[2] and w >= BOX[1] and e <= BOX[3]


def covers(x: int, y: int) -> bool:
    s, w, n, e = tile_box(TILE_Z, x, y)
    return s < FINLAND[2] and n > FINLAND[0] and w < FINLAND[3] and e > FINLAND[1]


def _first(props: dict, *names: str):
    for n in names:
        if props.get(n) not in (None, ""):
            return props[n]
    return None


def compact_ryhti(collection: dict) -> list[list]:
    """Ryhti's GeoJSON -> the same rows as compact(): [lat, lon, year, use, floors, address, facade, frame]."""
    rows = []
    for f in collection.get("features") or []:
        at = _centre(f.get("geometry"))
        if not at:
            continue
        p = f.get("properties") or {}
        done = _first(p, "completion_date", "completionDate", "valmistumispaiva", "construction_year")
        m = __import__("re").search(r"(1[5-9]\d\d|20\d\d)", str(done or ""))
        floors = _first(p, "number_of_storeys", "numberOfStoreys", "kerrosluku")
        try:
            floors = int(float(floors)) if floors not in (None, "") and float(floors) > 0 else None
        except (TypeError, ValueError):
            floors = None
        rows.append([round(at[0], 6), round(at[1], 6), int(m.group(1)) if m else None,
                     str(_first(p, "main_purpose", "mainPurpose", "kayttotarkoitus") or ""), floors,
                     str(_first(p, "address", "address_fin", "osoite") or ""),
                     str(_first(p, "facade_material", "facadeMaterial") or ""),
                     str(_first(p, "material_of_load_bearing_structures", "construction_method") or "")])
    return rows


class Buildings:
    def __init__(self, directory: Path, session: aiohttp.ClientSession, fake: bool = False):
        self.dir = directory
        self.session = session
        self.fake = fake
        self.failing: str | None = None
        self._limit = asyncio.Semaphore(3)
        self._ryhti: str | None = None  # the national register's collection name, once found

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
        if not in_hsy(x, y):
            return await self._fetch_ryhti(s, w, n, e)
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

    async def _fetch_ryhti(self, s: float, w: float, n: float, e: float) -> list[list]:
        names = ([self._ryhti] if self._ryhti else []) + [c for c in RYHTI_COLLECTIONS if c != self._ryhti]
        headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"}
        timeout = aiohttp.ClientTimeout(total=60)
        last: Exception | None = None
        for attempt in range(2):
            for name in names:
                url = f"{RYHTI}/collections/{name}/items"
                params = {"bbox": f"{w:.6f},{s:.6f},{e:.6f},{n:.6f}", "limit": "3000", "f": "application/geo+json"}
                async with self._limit, self.session.get(url, params=params, headers=headers, timeout=timeout) as resp:
                    if resp.status in (400, 404):
                        last = ValueError(f"HTTP {resp.status} for {name}")
                        continue
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                self._ryhti = name
                return compact_ryhti(data)
            if attempt:
                break
            # None of the names we know: ask the service what it calls its buildings.
            async with self.session.get(f"{RYHTI}/collections", params={"f": "application/json"}, headers=headers,
                                        timeout=timeout) as resp:
                resp.raise_for_status()
                listed = [c.get("id", "") for c in (await resp.json(content_type=None)).get("collections", [])]
            names = [c for c in listed if "building" in c.lower() and not any(
                bad in c.lower() for bad in ("address", "project", "permit", "part"))]
            _LOGGER.info("Building ages: the national register's collections are %s; trying %s", listed, names)
        raise last or ValueError("no building collection")

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
