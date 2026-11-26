"""Recent satellite pictures: Sentinel-2 (10 m) from the last two months, by day.

The "Recent" map type shows what a place looked like on one day a few days ago, not
the years-old mosaic of the Satellite view. Two Sentinel-2 satellites (three since
2025) pass over Finland every two to three days; most of those days are cloudy, so
the panel lists the days that have a picture where you are looking, each with how
much of the view it covers and how cloudy it was, and starts on the newest clear one.

Everything comes from Microsoft's Planetary Computer, free and without a key:

- the day list from its STAC catalogue (one search per area, kept for an hour),
- the pictures from its tiler: one day's granules as a mosaic (registered once per
  day; a registration expires after a while, answered 410, and is then redone),
  the least cloudy granule on top where two overlap. Its mosaics stop at zoom 9;
  further out the panel shrinks zoom-9 pictures. Zoom 13 is the satellite's own
  10 m, closer the panel enlarges.

The colours are lifted a little (gamma): the satellite's own true-colour picture is
made for the equator's sun and looks nearly black here in autumn and winter.

Tiles are kept on the NAS (own folder, own size cap, out of backups). A day's
picture doesn't change once it is in, but the newest two days can still get more
granules, so those are fetched again after a few hours.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import OrderedDict
from pathlib import Path

import aiohttp

from .config import USER_AGENT
from .tilecache import TileCache

_LOGGER = logging.getLogger(__name__)

PC = "https://planetarycomputer.microsoft.com/api"
STAC_SEARCH = f"{PC}/stac/v1/search"
REGISTER = f"{PC}/data/v1/mosaic/register"
COLLECTION = "sentinel-2-l2a"
TILE = (f"{PC}/data/v1/mosaic/{{sid}}/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}@1x.webp"
        f"?collection={COLLECTION}&assets=visual&nodata=0&color_formula={{cf}}")

DAYS_BACK = 60          # how far back the day list goes
MIN_Z, MAX_Z = 9, 13    # the tiler's mosaics start at 9; 13 is the satellite's own 10 m
MAX_BYTES = 300 * 1024 * 1024
FRESH_DAYS = 2          # days this new can still get more granules...
FRESH_AGE = 3 * 3600    # ...so their tiles are fetched again after this long
KEEP_AGE = 10 * 365 * 86400
SID_AGE = 6 * 3600      # a mosaic registration is reused this long
LIST_AGE = 3600         # a day list is reused this long
LIST_GRID = 0.25        # day lists are asked for areas snapped out to this grid (degrees)
SAMPLES = 9             # 9 x 9 points across the view to measure cover and cloud
CLEAR_CLOUD = 30        # a day counts as clear at or under this much cloud (%)...
CLEAR_COVER = 0.6       # ...covering at least this much of the view
REV = 1                 # bump when the picture's look changes (colour formula)
CONCURRENCY = 6

ATTRIBUTION = "Contains modified Copernicus Sentinel data, via Microsoft Planetary Computer"


def color_formula(day: str) -> str:
    """Lift the dark true-colour picture: more in the low sun of autumn and winter."""
    month = int(day[5:7])
    gamma = 2.1 if month in (11, 12, 1, 2) else 1.9 if month in (3, 10) else 1.7
    return f"Gamma RGB {gamma} Saturation 1.2"


def _inside_ring(lon: float, lat: float, ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def contains(geometry: dict | None, lon: float, lat: float) -> bool:
    """Whether a GeoJSON Polygon or MultiPolygon (lon, lat) holds the point."""
    if not geometry:
        return False
    polys = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon":
        polys = [polys]
    elif geometry.get("type") != "MultiPolygon":
        return False
    for poly in polys:
        if poly and _inside_ring(lon, lat, poly[0]) and not any(_inside_ring(lon, lat, h) for h in poly[1:]):
            return True
    return False


def summarize(items: list[dict], box: tuple[float, float, float, float], today: str | None = None) -> list[dict]:
    """The days with a picture in the box (south, west, north, east), newest first:
    {date, time (UTC, HH:MM), cloud (% of the covered part), cover (0..1), sats}.
    Cloud is each granule's own figure, taken from the least cloudy granule over each
    sample point (the one the mosaic puts on top)."""
    south, west, north, east = box
    points = [(west + (east - west) * (i + 0.5) / SAMPLES, south + (north - south) * (j + 0.5) / SAMPLES)
              for i in range(SAMPLES) for j in range(SAMPLES)]
    by_day: dict[str, list[dict]] = {}
    for it in items:
        when = (it.get("properties") or {}).get("datetime") or ""
        if len(when) >= 16:
            by_day.setdefault(when[:10], []).append(it)
    out = []
    for day, rows in by_day.items():
        covered, clouds = 0, 0.0
        for lon, lat in points:
            best = None
            for it in rows:
                cc = (it.get("properties") or {}).get("eo:cloud_cover")
                cc = 100.0 if cc is None else float(cc)
                if (best is None or cc < best) and contains(it.get("geometry"), lon, lat):
                    best = cc
            if best is not None:
                covered += 1
                clouds += best
        if not covered:
            continue
        times = sorted(it["properties"]["datetime"][11:16] for it in rows)
        sats = sorted({(it.get("id") or "")[:3] for it in rows if (it.get("id") or "").startswith("S2")})
        cloud = round(clouds / covered)
        cover = round(covered / len(points), 2)
        out.append({"date": day, "time": times[len(times) // 2], "cloud": cloud, "cover": cover, "sats": sats,
                    "clear": cloud <= CLEAR_CLOUD and cover >= CLEAR_COVER})
    out.sort(key=lambda d: d["date"], reverse=True)
    return out


def snap_box(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """The box snapped out to LIST_GRID, so nearby views share one catalogue search."""
    s, w, n, e = box
    g = LIST_GRID
    return (math.floor(s / g) * g, math.floor(w / g) * g, math.ceil(n / g) * g, math.ceil(e / g) * g)


class RecentSatellite:
    def __init__(self, directory: Path, session: aiohttp.ClientSession, fake: bool = False):
        self.dir = directory
        self.session = session
        self.fake = fake
        # Only used to keep the folder under its cap (prune); fetching is done here, since
        # the tiler answers in its own ways (204 nothing here, 410 registration expired).
        self.cache = TileCache(directory, session, MAX_BYTES)
        self.cache.default_age = KEEP_AGE
        self._limit = asyncio.Semaphore(CONCURRENCY)
        self._sids: dict[str, tuple[float, str]] = {}  # day -> (registered at, search id)
        self._sid_locks: dict[str, asyncio.Lock] = {}
        self._lists: OrderedDict[tuple, tuple[float, list[dict]]] = OrderedDict()
        self._pending: dict[Path, asyncio.Future] = {}
        self.failing: str | None = None  # the last error talking to the Planetary Computer

    # --- the days ------------------------------------------------------------------

    async def days(self, box: tuple[float, float, float, float]) -> dict:
        """What /api/recent/days answers: the days with a picture in the view."""
        south, west, north, east = box
        area = snap_box(box)
        now = time.time()
        hit = self._lists.get(area)
        if hit and now - hit[0] < LIST_AGE:
            items = hit[1]
        else:
            items = await (self._fake_items(area) if self.fake else self._search(area))
            self._lists[area] = (now, items)
            self._lists.move_to_end(area)
            while len(self._lists) > 24:
                self._lists.popitem(last=False)
        days = summarize(items, (south, west, north, east))
        best = next((d["date"] for d in days if d["clear"]), days[0]["date"] if days else None)
        return {"days": days, "best": best, "min_zoom": MIN_Z, "max_zoom": MAX_Z, "back": DAYS_BACK,
                "today": time.strftime("%Y-%m-%d", time.gmtime(now)), "attribution": ATTRIBUTION,
                "error": self.failing}

    async def _search(self, area: tuple[float, float, float, float]) -> list[dict]:
        s, w, n, e = area
        end = time.time()
        body = {
            "collections": [COLLECTION],
            "bbox": [w, s, e, n],
            "datetime": f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(end - DAYS_BACK * 86400))}/"
                        f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(end + 3600))}",
            "limit": 250,
            "sortby": [{"field": "datetime", "direction": "desc"}],
            "fields": {"include": ["id", "geometry", "properties.datetime", "properties.eo:cloud_cover"],
                       "exclude": ["assets", "links", "bbox", "stac_extensions"]},
        }
        items: list[dict] = []
        url, method = STAC_SEARCH, "POST"
        try:
            for _ in range(6):  # 250 a page; a wide view over two months can be a few pages
                async with self.session.request(
                    method, url, json=body if method == "POST" else None,
                    headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=40),
                ) as resp:
                    resp.raise_for_status()
                    page = await resp.json(content_type=None)
                for f in page.get("features") or []:
                    items.append({"id": f.get("id"), "geometry": f.get("geometry"), "properties": {
                        "datetime": (f.get("properties") or {}).get("datetime"),
                        "eo:cloud_cover": (f.get("properties") or {}).get("eo:cloud_cover")}})
                nxt = next((ln for ln in page.get("links") or [] if ln.get("rel") == "next"), None)
                if not nxt:
                    break
                url = nxt.get("href")
                method = (nxt.get("method") or "GET").upper()
                if method == "POST":
                    body = {**body, **(nxt.get("body") or {})}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            self.failing = f"catalogue: {err or type(err).__name__}"
            _LOGGER.warning("Recent satellite pictures: couldn't search the catalogue: %s", self.failing)
            if not items:
                raise
            return items
        self.failing = None
        return items

    # --- the pictures --------------------------------------------------------------

    def _source(self, day: str) -> str:
        return f"{day}-r{REV}"

    async def _sid(self, day: str, renew: bool = False) -> str:
        lock = self._sid_locks.setdefault(day, asyncio.Lock())
        async with lock:
            hit = self._sids.get(day)
            if hit and not renew and time.time() - hit[0] < SID_AGE:
                return hit[1]
            body = {
                "collections": [COLLECTION],
                "filter-lang": "cql2-json",
                "filter": {"op": "anyinteracts", "args": [
                    {"property": "datetime"}, {"interval": [f"{day}T00:00:00Z", f"{day}T23:59:59Z"]}]},
                "sortby": [{"field": "eo:cloud_cover", "direction": "asc"}],
            }
            async with self.session.post(REGISTER, json=body, headers={"User-Agent": USER_AGENT},
                                         timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
            sid = data.get("searchid") or data.get("id")
            if not sid:
                raise aiohttp.ClientError("no search id in the answer")
            self._sids[day] = (time.time(), sid)
            return sid

    async def tile(self, day: str, z: int, x: int, y: int) -> Path | None:
        """The day's picture tile on disk (WebP), or None where that day has none."""
        source = self._source(day)
        fresh = time.time() - time.mktime(time.strptime(day, "%Y-%m-%d")) < (FRESH_DAYS + 1) * 86400
        if fresh:
            self.cache.max_ages[source] = FRESH_AGE
        else:
            self.cache.max_ages.pop(source, None)
        path = self.dir / source / str(z) / str(x) / f"{y}.webp"
        try:
            st = path.stat()
            if time.time() - st.st_mtime < (FRESH_AGE if fresh else KEEP_AGE):
                return path if st.st_size else None
        except FileNotFoundError:
            pass
        if path in self._pending:
            return await asyncio.shield(self._pending[path])
        future = asyncio.get_running_loop().create_future()
        self._pending[path] = future
        try:
            result = await self._fetch(day, z, x, y, path)
        except BaseException:
            future.cancel()
            raise
        else:
            future.set_result(result)
            return result
        finally:
            del self._pending[path]

    async def _fetch(self, day: str, z: int, x: int, y: int, path: Path) -> Path | None:
        if self.fake:
            from .simrecent import fake_tile
            body = await asyncio.to_thread(fake_tile, day, z, x, y)
        else:
            try:
                body = await self._download(day, z, x, y)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                self.failing = f"pictures: {err or type(err).__name__}"
                _LOGGER.warning("Recent satellite picture %s %s/%s/%s failed: %s", day, z, x, y, self.failing)
                return path if path.exists() and path.stat().st_size else None
            self.failing = None
        tmp = path.with_suffix(".tmp")
        for _ in range(2):  # prune() may remove the folder between the two steps
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                tmp.write_bytes(body)
                os.replace(tmp, path)
                break
            except FileNotFoundError:
                continue
        return path if body else None

    async def _download(self, day: str, z: int, x: int, y: int) -> bytes:
        for attempt in range(2):
            sid = await self._sid(day, renew=attempt > 0)
            url = TILE.format(sid=sid, z=z, x=x, y=y, cf=color_formula(day).replace(" ", "+"))
            async with self._limit, self.session.get(url, headers={"User-Agent": USER_AGENT},
                                                     timeout=aiohttp.ClientTimeout(total=45)) as resp:
                if resp.status == 410:  # registration expired: register again, once
                    continue
                if resp.status in (204, 404):
                    return b""  # no picture here that day
                resp.raise_for_status()
                if not resp.content_type.startswith("image/"):
                    raise aiohttp.ClientError(f"not an image: {resp.content_type}")
                return await resp.read()
        raise aiohttp.ClientError("search registration keeps expiring")

    async def _fake_items(self, area) -> list[dict]:
        from .simrecent import fake_items
        return fake_items(area, DAYS_BACK)

    def prune(self) -> int:
        return self.cache.prune()

    def status(self) -> dict:
        when, size = self.cache.size_seen
        return {"mb": round(size / 1e6, 1) if when else None, "cap_mb": MAX_BYTES // 1_000_000, "error": self.failing}


def parse_box(text: str) -> tuple[float, float, float, float]:
    s, w, n, e = (float(v) for v in text.split(","))
    if not (-90 <= s < n <= 90 and -180 <= w < e <= 180) or n - s > 20 or e - w > 40:
        raise ValueError("bad box")
    return s, w, n, e

