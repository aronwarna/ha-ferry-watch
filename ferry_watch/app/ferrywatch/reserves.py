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
FILE_REV = 3  # cell files written with an older revision are fetched again (2: broken outlines closed, 3: seasons)
CREDIT = "© OpenStreetMap contributors (ODbL)"

Fetch = Callable[[str], Awaitable[Any]]

# The same reserves the drawn chart outlines (vchart.OSM_QUERIES["osm:reserve"]), plus
# national parks tagged only as such.
FILTERS = ('"leisure"="nature_reserve"', '"boundary"="protected_area"]["protect_class"~"^[1-5]$"',
           '"boundary"="national_park"',
           # OpenSeaMap's restricted areas for wildlife: bird and seal sanctuaries, often with
           # the season they're closed in.
           '"seamark:type"="restricted_area"]["seamark:restricted_area:category"~"sanctuary|reserve|bird|seal|game|wildlife|nature"')

# Seasonal no-go areas from the official register: SYKE's map service lists Metsähallitus'
# movement restriction areas of protected areas. The layer's name isn't fixed here: the first
# one whose name or title matches is used (found again daily), its features read in the
# cell's box as GeoJSON, and closing dates picked out of whatever fields they come in.
SYKE_WFS = "https://paikkatiedot.ymparisto.fi/geoserver/wfs"
SYKE_LAYER = re.compile(r"liikkumisrajoit|rörelsebegräns|movement.?restrict", re.I)
SYKE_CREDIT = "Movement restriction areas: SYKE, Metsähallitus (CC BY 4.0)"
SYKE_FIND_AGAIN = 86400

MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                          "nov", "dec"))}
MONTHS.update({"maj": 5, "okt": 10, "tam": 1, "hel": 2, "maa": 3, "huh": 4, "tou": 5, "kes": 6, "hei": 7,
               "elo": 8, "syy": 9, "lok": 10, "mar": 3, "jou": 12})
_DASH = r"\s*(?:-|–|—|to|till|until|asti|\.\.)\s*"
_RANGES = [
    # 1.4.–15.8. (Finnish and Swedish texts), 1.4-15.8
    (re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.?" + _DASH + r"(\d{1,2})\.(\d{1,2})\.?(?!\d)"), "dm"),
    # Apr 01-Aug 15 (OpenStreetMap's conditional restrictions), April 1 – August 15
    (re.compile(r"\b([A-Za-zäö]{3})[a-zäö]*\.?\s*(\d{1,2})" + _DASH + r"([A-Za-zäö]{3})[a-zäö]*\.?\s*(\d{1,2})\b"), "md"),
    # 1 Apr – 15 Aug, 1 april–15 augusti
    (re.compile(r"\b(\d{1,2})\.?\s*([A-Za-zäö]{3})[a-zäö]*" + _DASH + r"(\d{1,2})\.?\s*([A-Za-zäö]{3})[a-zäö]*\b"), "dM"),
    # --04-01/--08-15 (S-57 periods), 04-01–08-15
    (re.compile(r"(?:--)?\b(\d{2})-(\d{2})\b" + r"\s*(?:/|-|–|to)\s*" + r"(?:--)?(\d{2})-(\d{2})\b"), "mdmd"),
]
LANDING = re.compile(r"maihinnousu|landstigning|landing|nousta maihin", re.I)
NO_ENTRY = re.compile(r"liikkumi|oleskel|pääsy|rörelse|vistelse|access|entry|tillträde|no_entry|entry_prohibited", re.I)

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
    cat = tags.get("seamark:restricted_area:category", "")
    if tags.get("seamark:type") == "restricted_area" and not tags.get("protection_title"):
        return {"bird_sanctuary": "Bird sanctuary", "seal_sanctuary": "Seal sanctuary",
                "game_reserve": "Game reserve"}.get(cat.split(";")[0], "Restricted area (wildlife)")
    title = tags.get("protection_title", "").casefold()
    for pattern, kind in TITLES:
        if re.search(pattern, title):
            return kind
    if tags.get("boundary") == "national_park" or tags.get("protect_class") == "2":
        return "National park"
    if title:
        return tags["protection_title"][:1].upper() + tags["protection_title"][1:80]
    return "Nature reserve"


def _md(m: int, d: int) -> str | None:
    return f"{m:02d}-{d:02d}" if 1 <= m <= 12 and 1 <= d <= 31 else None


def date_ranges(text: str) -> list[tuple[str, str]]:
    """Yearly date ranges ("MM-DD", "MM-DD") written in a text in any of the usual ways."""
    out: list[tuple[str, str]] = []
    for pattern, order in _RANGES:
        for m in pattern.finditer(text or ""):
            g = m.groups()
            try:
                if order == "dm":
                    a, b = _md(int(g[1]), int(g[0])), _md(int(g[3]), int(g[2]))
                elif order == "md":
                    a, b = _md(MONTHS.get(g[0].lower(), 0), int(g[1])), _md(MONTHS.get(g[2].lower(), 0), int(g[3]))
                elif order == "dM":
                    a, b = _md(MONTHS.get(g[1].lower(), 0), int(g[0])), _md(MONTHS.get(g[3].lower(), 0), int(g[2]))
                else:
                    a, b = _md(int(g[0]), int(g[1])), _md(int(g[2]), int(g[3]))
            except ValueError:
                continue
            if a and b and a != b and (a, b) not in out:
                out.append((a, b))
        if out:
            break  # one way of writing per text
    return out


def _what(text: str, default: str = "No access") -> str:
    if LANDING.search(text or ""):
        return "No landing"
    if NO_ENTRY.search(text or ""):
        return "No access"
    return default


def seasons_from(texts: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Closed seasons [{"what", "from", "to"}] from (default what, text) pairs."""
    out: list[dict[str, str]] = []
    for default, text in texts:
        for a, b in date_ranges(text):
            what = _what(text, default)
            if not any(x["from"] == a and x["to"] == b for x in out):
                out.append({"what": what, "from": a, "to": b})
    return out[:4]


def _seasons(tags: dict[str, str]) -> list[dict[str, str]]:
    """When a reserve or sanctuary is closed each year, from OpenStreetMap's tags."""
    texts: list[tuple[str, str]] = []
    for key, what in (("access:conditional", "No access"), ("foot:conditional", "No access"),
                      ("boat:conditional", "No access by boat"), ("landing:conditional", "No landing")):
        for rule in re.split(r";\s*(?![^(]*\))", tags.get(key, "")):
            m = re.match(r"^\s*(no|private)\s*@\s*\(?(.+?)\)?\s*$", rule)
            if m:
                texts.append((what, m[2]))
    st = tags.get("seamark:restricted_area:restriction", "")
    st_what = "No landing" if "landing" in st else "No access" if "entry" in st else "Restricted"
    start = next((v for k, v in tags.items() if k.startswith("seamark") and k.endswith("period_start")), "")
    end = next((v for k, v in tags.items() if k.startswith("seamark") and k.endswith("period_end")), "")
    if start and end:
        texts.append((st_what, f"{start}/{end}"))
    for key in ("seasonal", "description", "description:en", "description:fi", "description:sv", "note",
                "seamark:information", "seamark:restricted_area:information", "opening_hours"):
        if tags.get(key):
            texts.append((st_what if st else "No access", tags[key]))
    return seasons_from(texts)


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
            if m and not (m[1] in ("no", "private") and date_ranges(m[2])):
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
        shape = _shape(outer, inner)
        if shape is None:
            continue
        name = (tags.get("name") or tags.get("name:fi") or tags.get("name:sv") or tags.get("seamark:name") or "").strip()
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
            "seasons": _seasons(tags),
            "source": "osm",
            **shape,
        }
    return list(out.values())


def _shape(outer: list[list], inner: list[list]) -> dict[str, Any] | None:
    """area_m2, bbox and simplified [lat, lon] rings from (lon, lat) rings."""
    area = sum(_ring_area_m2(r) for r in outer) - sum(_ring_area_m2(r) for r in inner)
    rings = []
    for r in outer + inner:
        s = _simplify(r, SIMPLIFY_M)
        if len(s) >= 4:
            rings.append([[round(lat, 5), round(lon, 5)] for lon, lat in s])
    if not rings:
        return None
    lons = [p[1] for r in rings for p in r]
    lats = [p[0] for r in rings for p in r]
    return {"area_m2": round(area), "bbox": [min(lons), min(lats), max(lons), max(lats)], "rings": rings}


def _feature_rings(geom: dict) -> list[tuple[list, list]]:
    """(outer, inner) lon/lat rings of a GeoJSON Polygon or MultiPolygon, one per polygon."""
    t, co = (geom or {}).get("type"), (geom or {}).get("coordinates") or []
    polys = [co] if t == "Polygon" else co if t == "MultiPolygon" else []
    out = []
    for poly in polys:
        rings = [[(float(p[0]), float(p[1])) for p in ring] for ring in poly if len(ring) >= 4]
        if rings:
            out.append(([rings[0]], rings[1:]))
    return out


def parse_restrictions(data: Any, layer: str) -> list[dict[str, Any]]:
    """Movement restriction areas from the register's GeoJSON: name and closed seasons
    picked out of whatever fields the layer has (dates either as one text, or as start and
    end fields)."""
    out = []
    for n, feat in enumerate((data or {}).get("features") or []):
        props = {str(k): v for k, v in (feat.get("properties") or {}).items() if v not in (None, "")}
        pieces = _feature_rings(feat.get("geometry"))
        if not pieces:
            continue
        name = next((str(v) for k, v in props.items() if re.search(r"nimi|name|namn", k, re.I)
                     and isinstance(v, str)), "")
        texts = " ".join(str(v) for v in props.values() if isinstance(v, str))
        what = _what(texts + " " + " ".join(props), "No access")
        pairs = [(what, str(v)) for v in props.values() if isinstance(v, str)]
        start = next((v for k, v in props.items() if re.search(r"alk|start|from|begin", k, re.I)), None)
        end = next((v for k, v in props.items() if re.search(r"lopp|end|until|slut|to$", k, re.I)), None)
        for v, slot in ((start, 0), (end, 1)):
            if isinstance(v, str):
                m = re.match(r"^(?:\d{4})?-?(\d{2})-(\d{2})", v) or re.match(r"^(\d{1,2})\.(\d{1,2})", v)
                if m and slot == 0:
                    start = (int(m[1]), int(m[2])) if "-" in v[:5] else (int(m[2]), int(m[1]))
                elif m:
                    end = (int(m[1]), int(m[2])) if "-" in v[:5] else (int(m[2]), int(m[1]))
        if isinstance(start, tuple) and isinstance(end, tuple):
            a, b = _md(*start), _md(*end)
            if a and b:
                pairs.insert(0, (what, f"--{a}/--{b}"))
        seasons = seasons_from(pairs)
        fid = str(feat.get("id") or n)
        for k, (outer, inner) in enumerate(pieces):
            shape = _shape(outer, inner)
            if shape is None:
                continue
            out.append({
                "id": f"s{re.sub(r'[^A-Za-z0-9_.-]', '', fid)[:40]}-{k}", "name": name[:120] or None, "other_names": [],
                "kind": "Movement restriction area", "iucn": None, "title": None, "ref": None, "since": None,
                "operator": "Metsähallitus", "access": [] if seasons else ["No access"], "description": None,
                "website": None, "wikipedia": None, "wikidata": None, "seasons": seasons, "source": "syke",
                "layer": layer, **shape,
            })
    return out


class Reserves:
    def __init__(self, folder: Path, session: aiohttp.ClientSession | None, fetch: Fetch | None = None,
                 wfs: Callable[[dict[str, str]], Awaitable[bytes]] | None = None):
        self.dir = folder
        self.session = session
        self.fetch = fetch or self._get
        # The register's map service; in the simulator a stand-in. Without either, none.
        self.wfs = wfs or (self._get_wfs if session is not None else None)
        self._syke: str | None = None  # the movement restriction layer's name, once found
        self._syke_at = 0.0  # when it was last looked for (or failed)
        self.syke_error: str | None = None
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

    async def _get_wfs(self, params: dict[str, str]) -> bytes:
        async with self.session.get(SYKE_WFS, params=params, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            if resp.status != 200:
                text = (await resp.content.read(400)).decode("utf-8", "replace")
                raise ValueError(f"HTTP {resp.status}: {re.sub(r'<[^>]+>', ' ', text).strip()[:200]}")
            body = await resp.content.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("answer too big")
            return body

    async def _syke_layer(self) -> str | None:
        """The register's movement restriction layer, looked for in the service's list once
        a day (after a failure, again after RETRY)."""
        if self.wfs is None:
            return None
        now = time.time()
        if now - self._syke_at < (SYKE_FIND_AGAIN if self.syke_error is None else RETRY):
            return self._syke
        self._syke_at = now
        try:
            caps = (await self.wfs({"service": "WFS", "version": "2.0.0", "request": "GetCapabilities"})).decode(
                "utf-8", "replace")
            found = None
            for block in re.findall(r"<(?:\w+:)?FeatureType\b.*?</(?:\w+:)?FeatureType>", caps, re.S):
                name = re.search(r"<(?:\w+:)?Name>([^<]+)<", block)
                title = re.search(r"<(?:\w+:)?Title>([^<]+)<", block)
                if name and SYKE_LAYER.search(f"{name[1]} {title[1] if title else ''}"):
                    found = name[1].strip()
                    break
            if found != self._syke or self.syke_error:
                _LOGGER.info("Movement restriction areas (SYKE): %s", f"layer {found}" if found
                             else "no such layer listed; only OpenStreetMap's seasons are shown")
            self._syke, self.syke_error = found, None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as err:
            if self.syke_error is None:
                _LOGGER.warning("Could not read SYKE's map service list: %s", err)
            self.syke_error = str(err) or type(err).__name__
        return self._syke

    async def _restrictions(self, cell: tuple[int, int]) -> tuple[list[dict], bool]:
        """The register's movement restriction areas in a cell, and whether that worked."""
        layer = await self._syke_layer()
        if layer is None:
            return [], self.syke_error is None
        s, w, n, e = cell_box(*cell)
        try:
            body = await self.wfs({"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": layer,
                                   "outputFormat": "application/json", "srsName": "EPSG:4326",
                                   "bbox": f"{w},{s},{e},{n},EPSG:4326", "count": "500"})
            got = await asyncio.to_thread(lambda: parse_restrictions(json.loads(body), layer))
            if got and not getattr(self, "_syke_logged", False):
                self._syke_logged = True
                _LOGGER.info("Movement restriction areas (SYKE) e.g. %s: %s", got[0]["name"], got[0]["seasons"])
            return got, True
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as err:
            if self.syke_error is None:
                _LOGGER.warning("Could not download movement restriction areas from SYKE: %s", err)
            self.syke_error = str(err) or type(err).__name__
            return [], False

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
            # Without the register's areas (it failed): fetched again sooner.
            got = (float(saved["updated"]) - (0 if saved.get("complete", True) else REFRESH - 6 * 3600),
                   saved["reserves"])
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

    def _save(self, cell: tuple[int, int], reserves: list[dict], updated: float, complete: bool = True) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(cell)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"rev": FILE_REV, "updated": updated, "complete": complete,
                       "source": "OpenStreetMap via Overpass API; SYKE (movement restriction areas)",
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
                extra, complete = await self._restrictions(cell)
                reserves += extra
                now = time.time()
                await asyncio.to_thread(self._save, cell, reserves, now, complete)
                self._remember(cell, (now - (0 if complete else REFRESH - 6 * 3600), reserves))
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
            return {"reserves": [], "pending": False, "error": "zoom in", "credit": CREDIT, "register": self._syke,
                    "register_error": self.syke_error}
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
        found = list(seen.values())
        credit = CREDIT + (f"; {SYKE_CREDIT}" if any(r.get("source") == "syke" for r in found) else "")
        return {"reserves": found, "pending": any(c in self._queue for c in missing),
                "error": self.error if missing else None, "credit": credit,
                "register": self._syke, "register_error": self.syke_error}
