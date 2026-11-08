"""Nature reserves and national parks from OpenStreetMap (© OpenStreetMap contributors, ODbL),
with what OpenStreetMap knows about each: names, kind, size, year protected, manager,
access rules and links. The panel uses them to highlight a reserve that's tapped, or all of
them in view, and to show its card. (Finland's reserves in OpenStreetMap were imported from
SYKE's register of protected areas, so most carry its id.)

The same reserves as the drawn chart's green dashed borders (vchart OSM_QUERIES
"osm:reserve"). Fetched by cell (about 22 x 22 km) when the panel first asks for one, into a
small gzip JSON file per cell, again after REFRESH. Outlines are simplified to a few metres.
A few cells are kept in memory while asked for. Nothing runs while no one looks.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import aiohttp

from .config import USER_AGENT

_LOGGER = logging.getLogger(__name__)

OVERPASS = "https://overpass-api.de/api/interpreter"
CELL_LAT = 0.2  # degrees; a cell is about 22 km each way at 60° N
CELL_LON = 0.4
MAX_CELLS = 24  # per request (a desktop view at zoom 10 is about 15)
REFRESH = 14 * 86400
RETRY = 900
TIMEOUT = 120
MAX_BYTES = 48 * 1024 * 1024
KEEP = 8  # cells kept parsed in memory
SIMPLIFY_M = 6  # outline points within this of the line between their neighbours are dropped
FILE_REV = 2  # cell files written with an older revision are fetched again (2: broken outlines closed)
CREDIT = "© OpenStreetMap contributors (ODbL)"

Fetch = Callable[[str], Awaitable[Any]]

# The same reserves the drawn chart outlines (vchart.OSM_QUERIES["osm:reserve"]), plus
# national parks tagged only as such.
FILTERS = ('"leisure"="nature_reserve"', '"boundary"="protected_area"]["protect_class"~"^[1-5]$"',
           '"boundary"="national_park"')

# protection_title (as SYKE's import wrote it, in Finnish or Swedish) -> what it is.
TITLES = [
    (r"kansallispuisto|nationalpark", "National park"),
    (r"luonnonpuisto|naturpark", "Strict nature reserve"),
    (r"yksityi|privat", "Private nature reserve"),
    (r"natura", "Natura 2000 area"),
    (r"hylje|säl", "Seal sanctuary"),
    (r"lehtojensuojelu", "Herb-rich forest reserve"),
    (r"soidensuojelu", "Mire reserve"),
    (r"vanhojen metsien|aarnimetsä", "Old-growth forest reserve"),
    (r"lintuvesi|fågel", "Bird sanctuary"),
    (r"luonnonsuojelualue|naturskyddsområde", "Nature reserve"),
]
IUCN = {"1": "Strict nature reserve", "1a": "Strict nature reserve", "1b": "Wilderness area", "2": "National park",
        "3": "Natural monument", "4": "Habitat or species area", "5": "Protected landscape or seascape"}
ACCESS = {"no": "No access", "private": "No access without permission", "permissive": "Access allowed",
          "yes": "Access allowed", "destination": "Access only to get there", "permit": "Permit needed"}


def overpass_query(s: float, w: float, n: float, e: float) -> str:
    b = f"({s:.4f},{w:.4f},{n:.4f},{e:.4f})"
    parts = "".join(f"nwr[{f}]{b};" for f in FILTERS)
    return f"[out:json][timeout:{TIMEOUT - 20}];({parts});out tags geom;"


def cell_of(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / CELL_LAT), math.floor(lon / CELL_LON)


def cell_box(cy: int, cx: int) -> tuple[float, float, float, float]:
    """(south, west, north, east)"""
    return cy * CELL_LAT, cx * CELL_LON, (cy + 1) * CELL_LAT, (cx + 1) * CELL_LON


def _link(text: str | None) -> str | None:
    text = (text or "").split(";")[0].strip()
    if not text or len(text) > 300:
        return None
    if not re.match(r"https?://", text):
        text = f"https://{text}"
    return text if re.match(r"https?://[\w.-]+\.[a-z]{2,}(/|$|\?)", text, re.I) else None


def _wikipedia(text: str | None) -> str | None:
    m = re.match(r"^([a-z]{2,3}):(.+)$", (text or "").strip())
    if not m:
        return None
    return f"https://{m[1]}.wikipedia.org/wiki/{quote(m[2].strip().replace(' ', '_'))}"


def _kind(tags: dict[str, str]) -> str:
    title = tags.get("protection_title", "").casefold()
    for pattern, kind in TITLES:
        if re.search(pattern, title):
            return kind
    if tags.get("boundary") == "national_park" or tags.get("protect_class") == "2":
        return "National park"
    if title:
        return tags["protection_title"][:1].upper() + tags["protection_title"][1:80]
    return "Nature reserve"


def _access(tags: dict[str, str]) -> list[str]:
    """Access rules as short lines: "No access 1 Apr–15 Aug", "Landing: no"."""
    out = []
    plain = tags.get("access", "").split(";")[0]
    if plain in ACCESS and plain not in ("yes", "permissive"):
        out.append(ACCESS[plain])
    for key, what in (("access:conditional", ""), ("foot:conditional", "On foot: "), ("boat:conditional", "Boats: "),
                      ("landing:conditional", "Landing: ")):
        for rule in re.split(r";\s*(?![^(]*\))", tags.get(key, "")):
            m = re.match(r"^\s*(\w+)\s*@\s*\(?(.+?)\)?\s*$", rule)
            if m:
                label = ACCESS.get(m[1], m[1])
                out.append(f"{what}{label.lower() if what else label} {m[2].replace('-', '–')}".strip())
    for key, label in (("boat", "Boats"), ("landing", "Landing"), ("seasonal", "Seasonal")):
        if tags.get(key) in ("no", "yes") and not (key == "seasonal" and tags[key] == "no"):
            out.append(f"{label}: {tags[key]}")
    return out[:4]


def _simplify(points: list[tuple[float, float]], tol_m: float) -> list[tuple[float, float]]:
    """Douglas-Peucker on (lon, lat), in metres at the ring's latitude; rings stay closed."""
    if len(points) <= 4:
        return points
    kx = 111320 * math.cos(math.radians(points[0][1]))
    ky = 110540
    xy = [(lon * kx, lat * ky) for lon, lat in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    tol2 = tol_m * tol_m
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        best, at = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            if ll == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, at = d, i
        if best > tol2:
            keep[at] = True
            stack += [(a, at), (at, b)]
    if points[0] == points[-1] and sum(keep) < 4:
        # A closed ring that simplified to a line: keep its farthest points.
        far = max(range(1, len(points) - 1), key=lambda i: (xy[i][0] - xy[0][0]) ** 2 + (xy[i][1] - xy[0][1]) ** 2)
        keep[far] = True
        keep[max(1, far // 2)] = True
    return [p for p, k in zip(points, keep) if k]


def _join(ways: list[list]) -> list[list]:
    from .foreign import join_ways  # the same stitching the chart uses for coastlines

    return join_ways(ways)


def _close(chains: list[list]) -> list[list]:
    """Rings from joined ways, mending broken outlines as OpenStreetMap sometimes has them
    (a member way missing or ending a little short of the next, or a reserve drawn as an
    open way): the open pieces are chained end to nearest end and the last one closed."""
    rings = [list(c) for c in chains if len(c) >= 4 and c[0] == c[-1]]
    open_ = [list(c) for c in chains if len(c) >= 2 and c[0] != c[-1]]
    while open_:
        cur = open_.pop(0)
        while open_:
            end = cur[-1]
            best, k_best, flip = math.inf, -1, False
            for k, w in enumerate(open_):
                for f, p in ((False, w[0]), (True, w[-1])):
                    d = (p[0] - end[0]) ** 2 + (p[1] - end[1]) ** 2
                    if d < best:
                        best, k_best, flip = d, k, f
            # Nearer to closing on itself than to any other piece: this ring is done.
            if (cur[0][0] - end[0]) ** 2 + (cur[0][1] - end[1]) ** 2 < best:
                break
            w = open_.pop(k_best)
            cur += w[::-1] if flip else w
        if len(cur) >= 3:
            rings.append(cur + [cur[0]])
    return rings


def _ring_area_m2(ring: list[tuple[float, float]]) -> float:
    if len(ring) < 3:
        return 0.0
    lat0 = ring[0][1]
    kx = 111320 * math.cos(math.radians(lat0))
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        s += (x1 * kx) * (y2 * 110540) - (x2 * kx) * (y1 * 110540)
    return abs(s) / 2


def parse_overpass(data: Any) -> list[dict[str, Any]]:
    """Reserves as dicts for the panel from an Overpass JSON answer (`out tags geom`)."""
    out: dict[str, dict[str, Any]] = {}
    for el in (data or {}).get("elements") or []:
        tags = el.get("tags") or {}
        kind = el.get("type")
        if kind == "way":
            g = [(p["lon"], p["lat"]) for p in el.get("geometry") or [] if p]
            outer, inner = _close([g]), []
        elif kind == "relation":
            ways = {"outer": [], "inner": []}
            for m in el.get("members") or ():
                if m.get("type") != "way" or not m.get("geometry"):
                    continue
                pts = [(p["lon"], p["lat"]) for p in m["geometry"] if p]
                ways["inner" if m.get("role") == "inner" else "outer"].append(pts)
            outer, inner = _join(ways["outer"]), _join(ways["inner"])
            outer, inner = _close(outer), _close(inner)
        else:
            continue  # a reserve as a single node has no area to highlight
        if not outer:
            _LOGGER.info("Nature reserve %s%s (%s) has no outline to fill", kind[0], el.get("id"), tags.get("name", "unnamed"))
            continue
        area = sum(_ring_area_m2(r) for r in outer) - sum(_ring_area_m2(r) for r in inner)
        rings = []
        for r in outer + inner:
            s = _simplify(r, SIMPLIFY_M)
            if len(s) >= 4:
                rings.append([[round(lat, 5), round(lon, 5)] for lon, lat in s])
        if not rings:
            continue
        lons = [p[1] for r in rings for p in r]
        lats = [p[0] for r in rings for p in r]
        name = (tags.get("name") or tags.get("name:fi") or tags.get("name:sv") or "").strip()
        names = [n for n in (tags.get("name:fi"), tags.get("name:sv")) if n and n.strip() != name]
        rid = f"{kind[0]}{el.get('id', 0)}"
        out[rid] = {
            "id": rid,
            "name": name[:120] or None,
            "other_names": [n.strip()[:120] for n in names][:2],
            "kind": _kind(tags),
            "iucn": IUCN.get(tags.get("protect_class", "").lower()),
            "title": tags.get("protection_title", "")[:80] or None,
            "ref": (tags.get("ref") or tags.get("ref:ysa") or tags.get("ref:WDPA") or "")[:40] or None,
            "since": (tags.get("start_date") or "")[:20] or None,
            "operator": (tags.get("operator") or tags.get("owner") or "")[:80] or None,
            "access": _access(tags),
            "description": (tags.get("description") or tags.get("description:en") or "")[:400] or None,
            "website": _link(tags.get("website") or tags.get("contact:website") or tags.get("url")),
            "wikipedia": _wikipedia(tags.get("wikipedia")),
            "wikidata": tags["wikidata"] if re.match(r"^Q\d+$", tags.get("wikidata", "")) else None,
            "area_m2": round(area),
            "bbox": [min(lons), min(lats), max(lons), max(lats)],
            "rings": rings,
        }
    return list(out.values())


class Reserves:
    def __init__(self, folder: Path, session: aiohttp.ClientSession | None, fetch: Fetch | None = None):
        self.dir = folder
        self.session = session
        self.fetch = fetch or self._get
        self._cells: OrderedDict[tuple[int, int], tuple[float, list[dict]]] = OrderedDict()
        self._queue: list[tuple[int, int]] = []
        self._failed: dict[tuple[int, int], float] = {}
        self._task: asyncio.Task | None = None
        self.error: str | None = None

    async def _get(self, query: str) -> Any:
        async with self.session.post(OVERPASS, data={"data": query}, headers={"User-Agent": USER_AGENT},
                                     timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            if resp.status != 200:
                text = (await resp.content.read(400)).decode("utf-8", "replace")
                raise ValueError(f"HTTP {resp.status}: {re.sub(r'<[^>]+>', ' ', text).strip()[:200]}")
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("answer too big")
                chunks.append(chunk)
        return json.loads(b"".join(chunks))

    def _path(self, cell: tuple[int, int]) -> Path:
        return self.dir / f"{cell[0]}_{cell[1]}.json.gz"

    def _read(self, cell: tuple[int, int]) -> tuple[float, list[dict]] | None:
        """(when fetched, reserves) for a cell, from memory or disk; None if never fetched."""
        if cell in self._cells:
            self._cells.move_to_end(cell)
            return self._cells[cell]
        try:
            with gzip.open(self._path(cell), "rt", encoding="utf-8") as f:
                saved = json.load(f)
            if saved.get("rev") != FILE_REV:
                return None
            got = (float(saved["updated"]), saved["reserves"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as err:
            _LOGGER.warning("Could not read %s: %s", self._path(cell), err)
            return None
        self._remember(cell, got)
        return got

    def _remember(self, cell: tuple[int, int], got: tuple[float, list[dict]]) -> None:
        self._cells[cell] = got
        self._cells.move_to_end(cell)
        while len(self._cells) > KEEP:
            self._cells.popitem(last=False)

    def _save(self, cell: tuple[int, int], reserves: list[dict], updated: float) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(cell)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"rev": FILE_REV, "updated": updated, "source": "OpenStreetMap via Overpass API",
                       "licence": "ODbL", "reserves": reserves}, f, separators=(",", ":"), ensure_ascii=False)
        tmp.replace(path)

    async def _run(self) -> None:
        """Fetch the queued cells one at a time (Overpass gives each address a slot or two)."""
        while self._queue:
            cell = self._queue[0]
            try:
                data = await self.fetch(overpass_query(*cell_box(*cell)))
                reserves = await asyncio.to_thread(parse_overpass, data)
                del data
                now = time.time()
                await asyncio.to_thread(self._save, cell, reserves, now)
                self._remember(cell, (now, reserves))
                self._failed.pop(cell, None)
                self.error = None
                _LOGGER.debug("Nature reserves for cell %s: %s", cell, len(reserves))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as err:
                if self.error is None:
                    _LOGGER.warning("Could not download nature reserves from OpenStreetMap (Overpass): %s", err)
                self.error = str(err) or type(err).__name__
                self._failed[cell] = time.time()
            finally:
                if self._queue and self._queue[0] == cell:
                    self._queue.pop(0)

    def in_view(self, south: float, west: float, north: float, east: float) -> dict[str, Any]:
        """Reserves touching a box (each once), and whether some of it is still downloading."""
        c0, c1 = cell_of(south, west), cell_of(north, east)
        cells = [(cy, cx) for cy in range(c0[0], c1[0] + 1) for cx in range(c0[1], c1[1] + 1)]
        if len(cells) > MAX_CELLS:
            return {"reserves": [], "pending": False, "error": "zoom in", "credit": CREDIT}
        # Nearest the middle first.
        my, mx = cell_of((south + north) / 2, (west + east) / 2)
        cells.sort(key=lambda c: (c[0] - my) ** 2 + (c[1] - mx) ** 2)
        now = time.time()
        seen: dict[str, dict] = {}
        want = []
        for cell in cells:
            got = self._read(cell)
            if got is None or now - got[0] > REFRESH:
                if now - self._failed.get(cell, 0) > RETRY:
                    want.append(cell)
            if got is not None:
                for r in got[1]:
                    b = r["bbox"]
                    if b[0] <= east and b[2] >= west and b[1] <= north and b[3] >= south:
                        seen.setdefault(r["id"], r)
        # The view asked for now goes first; cells no longer in view drop out of the queue.
        running = self._queue[:1] if self._task and not self._task.done() else []
        self._queue = running + [c for c in want if c not in running]
        if self._queue and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run())
        missing = [c for c in cells if c not in self._cells and self._read(c) is None]
        return {"reserves": list(seen.values()), "pending": any(c in self._queue for c in missing),
                "error": self.error if missing else None, "credit": CREDIT}
