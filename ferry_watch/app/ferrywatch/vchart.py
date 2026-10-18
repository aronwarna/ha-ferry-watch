"""A seamless nautical chart drawn from Finland's open chart data.

Traficom's chart service is scanned paper chart sheets, so sheet edges and inset
boxes show where they meet. This draws the chart itself instead, the way apps
like Luotsi do, from:

- Traficom's depth data (depth areas, contours, soundings), and
- Väylävirasto's fairway data (fairway lines and areas, leading lines, buoys,
  beacons and lights).

Data is fetched lazily in blocks (zoom-11 map tiles, about 10 km across) the
first time part of the map is viewed, kept on the NAS for a month, and cut into
small map tiles on request. Land is whatever isn't water: the panel paints the
Finnish area in the land colour and draws the depth areas over it, so islands
line up exactly with the depths around them.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

import aiohttp

from .charts import FINNISH_WATERS
from .config import USER_AGENT

_LOGGER = logging.getLogger(__name__)

TRAFICOM_WFS = "https://julkinen.traficom.fi/inspirepalvelu/rajoitettu/wfs"
TRAFICOM_OPEN_WFS = "https://julkinen.traficom.fi/inspirepalvelu/avoin/wfs"
VAYLA_WFS = "https://avoinapi.vaylapilvi.fi/vaylatiedot/vesivaylatiedot/wfs"
# National Land Survey topographic database (rocks, wrecks, names); needs a free API key.
MML_API = "https://avoin-paikkatieto.maanmittauslaitos.fi/maastotiedot/features/v1"
ATTRIBUTION = ("Chart data: Traficom (depths), Väylävirasto (fairways, aids), "
               "Maanmittauslaitos (rocks, names). Not for navigation.")

BLOCK_Z = 11  # blocks are zoom-11 map tiles, about 10 km across
MIN_Z = 10  # below this the panel shows Traficom's overview chart
UNITS = 65536  # block coordinates: 0..UNITS across a block, y down
EXTENT = 512  # tile coordinates sent to the panel: 0..EXTENT
TILE_BUFFER = 24  # tile units drawn past each edge, so lines and areas meet seamlessly
POINT_BUFFER = 64  # wider for points: symbols, light sectors and soundings reach further
MAX_AGE = 30 * 86400
FAILED_RETRY = 3600
PAGE = 20000
MAX_RESPONSE = 20 * 1024 * 1024  # bytes; more than this from one request is refused, to spare RAM
MEMORY_BLOCKS = 6  # parsed blocks kept in RAM; the rest are read from disk when needed
FORGET_AFTER = 20  # s; a queued block nobody has asked for again since is skipped
TILE_REV = 3  # bump when tiles change, so browsers don't keep drawing cached old ones
HALF = 20037508.342789244  # half the web-mercator world, in metres

# What to fetch: layer key -> (service, type name, kind, properties to keep)
SOURCES: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "depth": (TRAFICOM_WFS, "rajoitettu:DepthArea_A", "area", ("drval1", "drval2")),
    "contour": (TRAFICOM_WFS, "rajoitettu:DepthContour_L", "line", ("valdco",)),
    "sounding": (TRAFICOM_WFS, "rajoitettu:Sounding_P", "point", ("depth",)),
    "farea": (VAYLA_WFS, "vesivaylatiedot:vaylaalueet_uusi", "area", ("tyyppikoodi", "mitoitussyvays")),
    "fline": (VAYLA_WFS, "vesivaylatiedot:navigointilinjat_uusi", "line", ("mitoitussyvays",)),
    "lead": (VAYLA_WFS, "vesivaylatiedot:taululinjat_uusi", "line", ()),
    "aid": (VAYLA_WFS, "vesivaylatiedot:turvalaitteet_uusi", "point",
            ("turvalaitetyyppifi", "navigointilajikoodi", "valaistu", "nimifi")),
    "sector": (VAYLA_WFS, "vesivaylatiedot:valosektorit_uusi", "point", ("alkukulma", "loppukulma", "vari")),
    "shore": (TRAFICOM_OPEN_WFS, "avoin:ShorelineConstruction_L", "line", ()),
    "light": (VAYLA_WFS, "vesivaylatiedot:loistot_uusi", "point", ("viralvalotunnuse",)),
    "rock": (MML_API, "vesikivi", "point", ("kohdeluokka",)),
    "rockarea": (MML_API, "vesikivikko", "area", ()),
    "wreck": (MML_API, "hylky", "point", ("kohdeluokka",)),
    "name": (MML_API, "paikannimi", "point", ("teksti", "kieli", "kohdeluokka")),
}

# Each block is stored in parts, one file each, so a part added later (or one that
# needs an API key) is fetched on its own without downloading the rest again.
PARTS: dict[str, tuple[str, ...]] = {
    "": ("depth", "contour", "sounding", "farea", "fline", "lead", "aid", "sector"),
    "more": ("shore", "light"),
    "mml": ("rock", "rockarea", "wreck", "name"),
}

# Navigation aid types, as one letter for the panel.
AID_TYPES = {
    "viitta": "v", "poiju": "p", "reunamerkki": "r", "linjamerkki": "l", "kummeli": "k",
    "sektoriloisto": "s", "merimajakka": "m", "tunnusmajakka": "m", "suuntaloisto": "d",
    "apuloisto": "a", "tutkamerkki": "t", "tutkamajakka": "t",
}

# MML feature classes. Rocks: 1 underwater, 2 awash, 3 above water.
ROCK_TYPES = {38511: 1, 38512: 2, 38513: 3}
# Wrecks: 1 depth unknown (dangerous), 2 depth known, 3 hull showing.
WRECK_TYPES = {16721: 1, 16722: 2, 16712: 3}

# Zoom from which each layer is drawn.
LAYER_MIN_Z = {"depth": MIN_Z, "contour": MIN_Z, "sounding": 13, "farea": 11, "fline": MIN_Z,
               "lead": 11, "aid": 11, "sector": 12, "shore": 12, "light": 13,
               "rock": 12, "rockarea": 12, "wreck": 12, "name": 11}


# --- geometry ------------------------------------------------------------------------

def clip_ring(pts: list[tuple[float, float]], x0: float, y0: float, x1: float, y1: float) -> list:
    """Sutherland-Hodgman: the part of a polygon ring inside the box. Edges along the
    box are fine for filling, which is all rings are used for."""
    for edge in range(4):
        if not pts:
            return pts
        out = []
        px, py = pts[-1]
        for cx, cy in pts:
            if edge == 0:
                cin, pin = cx >= x0, px >= x0
            elif edge == 1:
                cin, pin = cx <= x1, px <= x1
            elif edge == 2:
                cin, pin = cy >= y0, py >= y0
            else:
                cin, pin = cy <= y1, py <= y1
            if cin != pin:
                if edge < 2:
                    xe = x0 if edge == 0 else x1
                    out.append((xe, py + (cy - py) * (xe - px) / (cx - px)))
                else:
                    ye = y0 if edge == 2 else y1
                    out.append((px + (cx - px) * (ye - py) / (cy - py), ye))
            if cin:
                out.append((cx, cy))
            px, py = cx, cy
        pts = out
    return pts


def clip_line(pts: list[tuple[float, float]], x0: float, y0: float, x1: float, y1: float) -> list[list]:
    """Liang-Barsky per segment: the pieces of a line inside the box."""
    parts: list[list] = []
    cur: list = []
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        t0, t1 = 0.0, 1.0
        ok = True
        for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                t = q / p
                if p < 0:
                    if t > t1:
                        ok = False
                        break
                    t0 = max(t0, t)
                else:
                    if t < t0:
                        ok = False
                        break
                    t1 = min(t1, t)
        if not ok:
            if len(cur) > 1:
                parts.append(cur)
            cur = []
            continue
        start = (ax + t0 * dx, ay + t0 * dy)
        end = (ax + t1 * dx, ay + t1 * dy)
        if not cur or cur[-1] != start:
            if len(cur) > 1:
                parts.append(cur)
            cur = [start]
        cur.append(end)
        if t1 < 1.0:
            parts.append(cur)
            cur = []
    if len(cur) > 1:
        parts.append(cur)
    return parts


def simplify(pts: list, tol: float) -> list:
    """Douglas-Peucker with a squared tolerance, keeping the end points."""
    n = len(pts)
    if n < 3:
        return pts
    keep = [False] * n
    keep[0] = keep[-1] = True
    tol2 = tol * tol
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        len2 = dx * dx + dy * dy
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if len2 == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, idx = d, i
        if best > tol2:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(pts, keep) if k]


def _bbox(pts) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _flat(pts) -> list[int]:
    """[(x, y), ...] -> [x0, y0, x1, y1, ...] as whole tile units, dropping repeats."""
    out: list[int] = []
    for x, y in pts:
        xi, yi = round(x), round(y)
        if len(out) >= 2 and out[-2] == xi and out[-1] == yi:
            continue
        out += (xi, yi)
    return out


# --- coordinates ---------------------------------------------------------------------

def block_bounds(bx: int, by: int) -> tuple[float, float, float, float]:
    """Web-mercator metres of a block: (minx, miny, maxx, maxy)."""
    size = 2 * HALF / 2**BLOCK_Z
    minx = -HALF + bx * size
    maxy = HALF - by * size
    return minx, maxy - size, minx + size, maxy


def to_block(bx: int, by: int) -> Callable[[float, float], tuple[float, float]]:
    minx, _, _, maxy = block_bounds(bx, by)
    scale = UNITS / (2 * HALF / 2**BLOCK_Z)
    return lambda x, y: ((x - minx) * scale, (maxy - y) * scale)


def lonlat_to_mercator(lon: float, lat: float) -> tuple[float, float]:
    x = lon * HALF / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) * HALF / math.pi
    return x, y


FINNISH_WATERS_M = [lonlat_to_mercator(lon, lat) for lon, lat in FINNISH_WATERS]
NEAR_M = 30000  # Finnish chart data reaches a little past FINNISH_WATERS


def near_finland(bounds: tuple[float, float, float, float]) -> bool:
    """Whether a block could hold Finnish chart data (it touches FINNISH_WATERS, give or take)."""
    minx, miny, maxx, maxy = bounds[0] - NEAR_M, bounds[1] - NEAR_M, bounds[2] + NEAR_M, bounds[3] + NEAR_M
    poly = FINNISH_WATERS_M
    if any(minx <= x <= maxx and miny <= y <= maxy for x, y in poly):
        return True
    for i in range(5):
        for j in range(5):
            px, py = minx + (maxx - minx) * i / 4, miny + (maxy - miny) * j / 4
            inside = False
            k = len(poly) - 1
            for n, (xi, yi) in enumerate(poly):
                xj, yj = poly[k]
                if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                    inside = not inside
                k = n
            if inside:
                return True
    return False


# Kept on the NAS when "Keep chart on NAS" is on: the Gulf of Finland, Hanko to the
# eastern border (lon/lat). Blocks in it with no Finnish waters nearby are left out.
KEEP_AREA = (22.7, 59.45, 28.2, 60.75)
KEEP_PAUSE = 1.0  # s between downloads, to be gentle on Traficom and Väylävirasto
KEEP_RECHECK = 6 * 3600  # s between passes that refresh blocks older than MAX_AGE


def keep_blocks(area: tuple[float, float, float, float] = KEEP_AREA) -> list[tuple[int, int]]:
    """Blocks to keep on disk for an area, nearest the middle of the gulf first."""
    n = 2**BLOCK_Z
    (x0, y1), (x1, y0) = lonlat_to_mercator(area[0], area[1]), lonlat_to_mercator(area[2], area[3])
    size = 2 * HALF / n
    bx0, bx1 = int((x0 + HALF) // size), int((x1 + HALF) // size)
    by0, by1 = int((HALF - y0) // size), int((HALF - y1) // size)
    return [(bx, by) for by in range(by0, by1 + 1) for bx in range(bx0, bx1 + 1)
            if near_finland(block_bounds(bx, by))]


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- turning WFS features into block data -------------------------------------------

def _rings(geom: dict) -> list[list]:
    """Polygon rings (outer and holes alike) of a Polygon or MultiPolygon."""
    if geom["type"] == "Polygon":
        return list(geom["coordinates"])
    if geom["type"] == "MultiPolygon":
        return [ring for poly in geom["coordinates"] for ring in poly]
    return []


def _lines(geom: dict) -> list[list]:
    if geom["type"] == "LineString":
        return [geom["coordinates"]]
    if geom["type"] == "MultiLineString":
        return list(geom["coordinates"])
    return []


def _points(geom: dict) -> list:
    if geom["type"] == "Point":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPoint":
        return list(geom["coordinates"])
    return []


def build_block(bx: int, by: int, layers: dict[str, list[dict]]) -> dict[str, list]:
    """Clip each layer's features to the block (with a margin) and keep only what the
    panel draws. Areas: [props, bbox, rings]; lines: [props, bbox, line]; points: [props, x, y]."""
    project = to_block(bx, by)
    margin = UNITS * TILE_BUFFER / EXTENT  # a block-sized tile's buffer, the largest needed
    lo, hi = -margin, UNITS + margin
    plo, phi = -UNITS * POINT_BUFFER / EXTENT, UNITS * (1 + POINT_BUFFER / EXTENT)
    out: dict[str, list] = {}
    for key, features in layers.items():
        kind = SOURCES[key][2]
        props_wanted = SOURCES[key][3]
        rows: list = []
        for f in features:
            geom = f.get("geometry")
            if not geom:
                continue
            raw = {k.lower(): v for k, v in (f.get("properties") or {}).items()}
            props = [raw.get(p) for p in props_wanted]
            if kind == "area":
                rings = []
                for ring in _rings(geom):
                    clipped = clip_ring([project(*c[:2]) for c in ring], lo, lo, hi, hi)
                    if len(clipped) >= 3:
                        rings.append([(round(x, 1), round(y, 1)) for x, y in clipped])
                if rings:
                    rows.append([props, _bbox([p for r in rings for p in r]), rings])
            elif kind == "line":
                for line in _lines(geom):
                    for part in clip_line([project(*c[:2]) for c in line], lo, lo, hi, hi):
                        part = [(round(x, 1), round(y, 1)) for x, y in part]
                        rows.append([props, _bbox(part), part])
            else:
                for c in _points(geom):
                    x, y = project(*c[:2])
                    if plo <= x <= phi and plo <= y <= phi:
                        rows.append([props, round(x, 1), round(y, 1)])
        out[key] = rows
    return out


# --- cutting tiles --------------------------------------------------------------------

def _depth_band(drval1: Any, drval2: Any) -> int:
    """0: drying, 1: 0-3 m, 2: 3-6 m, 3: 6-10 m, 4: deeper (white)."""
    d1 = _num(drval1)
    d2 = _num(drval2)
    if d1 is not None and d1 < 0 and (d2 is None or d2 <= 0):
        return 0
    top = d2 if d2 is not None else d1
    if top is None:
        return 4
    if top <= 3:
        return 1
    if top <= 6:
        return 2
    if top <= 10:
        return 3
    return 4


def make_tile(z: int, x: int, y: int, blocks: dict[tuple[int, int], dict | None]) -> dict[str, Any]:
    """The chart features for one map tile, in tile units (0..EXTENT, y down)."""
    tile: dict[str, Any] = {"e": EXTENT}
    lo, hi = -TILE_BUFFER, EXTENT + TILE_BUFFER
    tol = 0.35 if z >= 12 else 0.5
    land_rects: list[tuple[float, float, float, float]] = []
    seen_points: set = set()
    for (bx, by), data in blocks.items():
        if not data:
            continue
        # Block units -> tile units, and the tile (with buffer) in block units for quick skips.
        if z >= BLOCK_Z:
            n = 2 ** (z - BLOCK_Z)
            size = UNITS / n
            ox, oy = (x - bx * n) * size, (y - by * n) * size
        else:
            n = 2 ** (BLOCK_Z - z)
            size = UNITS * n
            ox, oy = (x * n - bx) * UNITS, (y * n - by) * UNITS
        k = EXTENT / size
        bx0, by0 = ox + lo / k, oy + lo / k
        bx1, by1 = ox + hi / k, oy + hi / k
        px0, py0 = ox - POINT_BUFFER / k, oy - POINT_BUFFER / k
        px1, py1 = ox + (EXTENT + POINT_BUFFER) / k, oy + (EXTENT + POINT_BUFFER) / k

        def conv(pts):
            return [((px - ox) * k, (py - oy) * k) for px, py in pts]

        def overlaps(bb):
            return bb[2] >= bx0 and bb[0] <= bx1 and bb[3] >= by0 and bb[1] <= by1

        if data.get("depth"):
            # Land is painted only where this block's depths are, so a block that
            # couldn't be fetched never shows up as solid land. (1 unit of overlap
            # hides the seam between blocks.)
            land_rects.append((max(lo, -ox * k - 1), max(lo, -oy * k - 1),
                               min(hi, (UNITS - ox) * k + 1), min(hi, (UNITS - oy) * k + 1)))

        for key, rows in data.items():
            if z < LAYER_MIN_Z.get(key, 99):
                continue
            kind = SOURCES[key][2]
            out = tile.setdefault(key, [])
            for row in rows:
                if kind == "point":
                    props, px, py = row
                    if not (px0 <= px <= px1 and py0 <= py <= py1):
                        continue
                    tx, ty = round((px - ox) * k), round((py - oy) * k)
                    seen = (key, tx, ty, *map(str, props))
                    if seen in seen_points:
                        continue  # the same point from two blocks' margins
                    seen_points.add(seen)
                    out.append([tx, ty, *props])
                    continue
                props, bb, geom = row
                if not overlaps(bb):
                    continue
                # Too small to see at this zoom
                if (bb[2] - bb[0]) * k < 0.6 and (bb[3] - bb[1]) * k < 0.6:
                    continue
                if kind == "area":
                    rings = []
                    for ring in geom:
                        part = simplify(clip_ring(conv(ring), lo, lo, hi, hi), tol)
                        if len(part) >= 3:
                            rings.append(_flat(part))
                    if not rings:
                        continue
                    if key == "depth":
                        out.append([_depth_band(*props), *rings])
                    else:
                        out.append([*props, *rings])
                else:
                    if key == "contour" and z < 12 and (_num(props[0]) or 0) < 10:
                        continue
                    if key == "fline" and z < 11 and props[0] is None:
                        continue  # boat routes only once zoomed in
                    for part in clip_line(conv(geom), lo, lo, hi, hi):
                        flat = _flat(simplify(part, tol))
                        if len(flat) >= 4:
                            out.append([*props, flat])
    if land_rects:
        # Land: the Finnish area, painted under the depth areas.
        minx, miny, maxx, maxy = _tile_mercator(z, x, y)
        k = EXTENT / (maxx - minx)
        ring = [((mx - minx) * k, (maxy - my) * k) for mx, my in FINNISH_WATERS_M]
        land = [_flat(part) for part in (clip_ring(ring, *rect) for rect in land_rects) if len(part) >= 3]
        if land:
            tile["land"] = land
    tile["depth"] = sorted(tile.get("depth", []), key=lambda row: -row[0])  # deep first
    if "sounding" in tile:
        tile["sounding"] = _thin_soundings(tile["sounding"])
    if "aid" in tile:
        tile["aid"] = [[px, py, AID_TYPES.get(str(t or "").lower(), "o"), _int(cat), 1 if lit == "K" else 0, name]
                       for px, py, t, cat, lit, name in tile["aid"]]
    if "sector" in tile:
        # Light sectors: [x, y, from°, to°, colour], bearings towards the light as charts give them.
        tile["sector"] = [[px, py, a, b, str(c or "v")] for px, py, a, b, c in
                          ((px, py, _num(a), _num(b), c) for px, py, a, b, c in tile["sector"])
                          if a is not None and b is not None and (b - a) % 360 not in (0,)]
    if "farea" in tile:
        tile["farea"] = [[_int(code), _num(draught), *rings] for code, draught, *rings in tile["farea"]]
    if "fline" in tile:
        tile["fline"] = [[_num(draught), line] for draught, line in tile["fline"]]
    if "contour" in tile:
        tile["contour"] = [[_num(v), line] for v, line in tile["contour"]]
    if "light" in tile:
        # Light characters as printed next to the light, e.g. "Iso WRG 4s".
        tile["light"] = [[px, py, str(t).strip()] for px, py, t in tile["light"] if t and str(t).strip() not in ("-", "")]
    if "rock" in tile:
        tile["rock"] = [[px, py, ROCK_TYPES.get(_int(code), 1)] for px, py, code in tile["rock"]]
    if "wreck" in tile:
        tile["wreck"] = [[px, py, WRECK_TYPES.get(_int(code), 1)] for px, py, code in tile["wreck"]]
    if "name" in tile:
        names = []
        for px, py, text, lang, code in tile["name"]:
            text = str(text or "")
            if not text or ("," in text and text.isupper()):
                continue  # e.g. "HANKO, POHJOINEN": station names, not chart names
            kind = _name_kind(_int(code))
            if z < 12 and kind != "s":
                continue  # only towns and villages when zoomed out
            names.append([px, py, str(text), kind, 0 if lang in (None, "fin", "fi") else 1])
        # Finnish first, so where both languages share a spot the panel keeps the Finnish one.
        tile["name"] = [row[:4] for row in sorted(names, key=lambda r: r[4])]
    return {k: v for k, v in tile.items() if v != []}


def _name_kind(code: int | None) -> str:
    """Name class for the panel: s settlement, w water, l land (islands, capes ...)."""
    if code is None:
        return "l"
    if 48000 <= code < 49000:
        return "s"
    if 36000 <= code < 37000:
        return "w"
    return "l"


def _int(value: Any) -> int | None:
    v = _num(value)
    return int(v) if v is not None else None


def _thin_soundings(rows: list, cell: int = 32) -> list:
    """Keep the shallowest sounding in each small square, as charts do. The squares line
    up across tiles (cell divides EXTENT and POINT_BUFFER), so neighbouring tiles keep
    the same soundings near their shared edge and none is cut in half."""
    best: dict[tuple[int, int], list] = {}
    for px, py, depth in rows:
        d = _num(depth)
        if d is None:
            continue
        key = (px // cell, py // cell)
        if key not in best or d < best[key][2]:
            best[key] = [px, py, round(d, 1)]
    return list(best.values())


def _tile_mercator(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = 2 * HALF / 2**z
    minx = -HALF + x * size
    maxy = HALF - y * size
    return minx, maxy - size, minx + size, maxy


def blocks_for(z: int, x: int, y: int) -> list[tuple[int, int]]:
    if z >= BLOCK_Z:
        s = z - BLOCK_Z
        return [(x >> s, y >> s)]
    n = 2 ** (BLOCK_Z - z)
    return [(x * n + i, y * n + j) for j in range(n) for i in range(n)]


# --- fetching and caching -------------------------------------------------------------

Fetcher = Callable[[str, tuple[float, float, float, float]], "asyncio.Future[list[dict]]"]


class _Skipped(Exception):
    pass


def mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    return x * 180 / HALF, math.degrees(2 * math.atan(math.exp(y * math.pi / HALF)) - math.pi / 2)


def _to_mercator(coords: Any) -> Any:
    """GeoJSON coordinates from lon/lat to web-mercator metres, at any nesting depth."""
    if coords and isinstance(coords[0], (int, float)):
        return lonlat_to_mercator(coords[0], coords[1])
    return [_to_mercator(c) for c in coords]


def _mml_features(page: dict) -> list[dict]:
    """MML features with lon/lat geometry converted, and the name text under one key."""
    out = []
    for f in page.get("features") or []:
        geom = f.get("geometry")
        if not geom or not geom.get("coordinates"):
            continue
        props = f.get("properties") or {}
        text = props.get("teksti") or props.get("nimi") or props.get("teksti_fin") or props.get("name")
        if isinstance(text, dict):  # a text with its language
            text = text.get("teksti") or text.get("text") or next(iter(text.values()), None)
        out.append({"geometry": {"type": geom["type"], "coordinates": _to_mercator(geom["coordinates"])},
                    "properties": {**props, "teksti": text}})
    return out


class VectorChart:
    def __init__(self, directory: Path, session: aiohttp.ClientSession | None,
                 fetch_layer: Callable[[str, tuple[float, float, float, float]], Any] | None = None,
                 mml_key: str = ""):
        self.dir = directory
        self.session = session
        self.fetch_layer = fetch_layer or self._fetch_layer
        self.mml_key = mml_key
        self._memory: OrderedDict[tuple[int, int], dict] = OrderedDict()
        self._pending: dict[tuple[int, int], asyncio.Task] = {}
        self._wanted: dict[tuple[int, int], float] = {}  # when a block was last asked for
        self._limit = asyncio.Semaphore(1)  # one block at a time: gentle on the services and on RAM
        self._cutting = asyncio.Semaphore(1)
        self._failed: dict[tuple[int, int, str], float] = {}
        self._logged_props: set[str] = set()
        self.errors: dict[str, str] = {}  # layer -> last error, for the panel
        self.blocks_fetched = 0
        self.keep_status: dict[str, Any] | None = None  # progress of keep(), None while it's off

    def set_mml_key(self, key: str) -> None:
        """A new Land Survey key: blocks are read again so rocks and names get fetched."""
        self.mml_key = key.strip()
        self._memory.clear()
        self._failed = {k: t for k, t in self._failed.items() if k[2] != "mml"}
        for layer in PARTS["mml"]:
            self.errors.pop(layer, None)

    async def check_mml_key(self, key: str) -> str | None:
        """None if the Land Survey accepts the key, else what went wrong."""
        if self.session is None:
            return None
        try:
            await self._get_json(f"{MML_API}/collections/vesikivi/items",
                                 {"bbox": "24.40,59.98,24.42,59.99", "limit": "1", "api-key": key.strip()}, "rock")
        except aiohttp.ClientError as err:
            return str(err)
        except (asyncio.TimeoutError, ValueError) as err:
            return str(err) or type(err).__name__
        return None

    def _parts(self) -> list[str]:
        return [part for part in PARTS if part != "mml" or self.mml_key]

    def _path(self, bx: int, by: int, part: str = "") -> Path:
        return self.dir / "blocks" / f"{bx}_{by}{'.' + part if part else ''}.json.gz"

    def status(self) -> dict[str, Any]:
        root = self.dir / "blocks"
        files = list(root.glob("*.json.gz")) if root.exists() else []
        return {"blocks": sum(1 for f in files if f.name.count(".") == 2),
                "mb": round(sum(f.stat().st_size for f in files) / 1e6, 1),
                "errors": self.errors, "attribution": ATTRIBUTION, "min_zoom": MIN_Z, "keep": self.keep_status,
                "mml_key": bool(self.mml_key), "rev": f"{TILE_REV}{''.join(p[:1] for p in self._parts())}"}

    def _fresh(self, key: tuple[int, int]) -> bool:
        for part in self._parts():
            try:
                age = time.time() - self._path(*key, part).stat().st_mtime
            except FileNotFoundError:
                return False
            if age >= MAX_AGE:
                return False
        return True

    async def keep(self, blocks: list[tuple[int, int]] | None = None) -> None:
        """Download every block of KEEP_AREA to disk, one at a time, and keep them fresh.

        Runs until cancelled. Panel requests still go first: they queue for the same
        one-download-at-a-time lock, and this only asks for its next block after the
        previous one is done.
        """
        blocks = keep_blocks() if blocks is None else blocks
        try:
            while True:
                done = sum(self._fresh(key) for key in blocks)
                self.keep_status = {"total": len(blocks), "done": done, "running": done < len(blocks)}
                for key in blocks:
                    if self._fresh(key):
                        continue
                    self._wanted[key] = math.inf  # never skipped as "the map moved on"
                    got = await asyncio.shield(self._block_task(key))
                    self.keep_status["done"] = sum(self._fresh(k) for k in blocks)
                    # A failed download backs off, so a service that's down isn't hammered.
                    await asyncio.sleep(KEEP_PAUSE if got is not None and self._fresh(key) else 60)
                self.keep_status["running"] = False
                await asyncio.sleep(KEEP_RECHECK)
        finally:
            self.keep_status = None

    async def tile(self, z: int, x: int, y: int,
                   wait: float | None = None) -> tuple[dict[str, Any], bool] | None:
        """The tile, and whether every block it needs was available.

        With ``wait``, gives up after that many seconds and returns None while the
        blocks keep downloading in the background: a browser only opens a few
        connections to Home Assistant, and a tile request held open for half a
        minute would stall the rest of the panel (and Home Assistant's own UI).
        """
        keys = blocks_for(z, x, y)
        now = time.monotonic()
        for key in keys:
            self._wanted[key] = now
        tasks = [self._block_task(key) for key in keys]
        pending = [t for t in tasks if not t.done()]
        if pending:
            done, still = await asyncio.wait([asyncio.shield(t) for t in pending], timeout=wait)
            if still:
                return None
        loaded = [t.result() for t in tasks]
        blocks = dict(zip(keys, loaded))
        async with self._cutting:  # one tile cut at a time keeps the web server responsive
            tile = await asyncio.to_thread(make_tile, z, x, y, blocks)
        wanted = [key for part in self._parts() for key in PARTS[part]]
        return tile, all(b is not None and all(k in b for k in wanted) for b in loaded)

    async def block(self, bx: int, by: int) -> dict | None:
        self._wanted[(bx, by)] = time.monotonic()
        return await asyncio.shield(self._block_task((bx, by)))

    def _block_task(self, key: tuple[int, int]) -> "asyncio.Future[dict | None]":
        if key in self._memory:
            self._memory.move_to_end(key)
            future = asyncio.get_running_loop().create_future()
            future.set_result(self._memory[key])
            return future
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(self._load(*key))
            self._pending[key] = task
            task.add_done_callback(lambda _t, key=key: self._done(key))
        return task

    def _done(self, key: tuple[int, int]) -> None:
        self._pending.pop(key, None)
        self._wanted.pop(key, None)

    async def _load(self, bx: int, by: int) -> dict | None:
        layers: dict[str, list] = {}
        for part in self._parts():
            got = await self._load_part(bx, by, part)
            if got is None:
                if part == "":
                    return None  # without depths there's no chart to draw
                continue
            layers.update(got)
        self._memory[(bx, by)] = layers
        while len(self._memory) > MEMORY_BLOCKS:
            self._memory.popitem(last=False)
        return layers

    async def _load_part(self, bx: int, by: int, part: str) -> dict | None:
        path = self._path(bx, by, part)
        cached = None
        try:
            if time.time() - path.stat().st_mtime < MAX_AGE:
                cached = await asyncio.to_thread(_read, path)
            # A part missing some data is tried again sooner.
            if cached and cached.get("missing") and time.time() - path.stat().st_mtime > FAILED_RETRY:
                cached = None
        except FileNotFoundError:
            pass
        if cached is None:
            if time.time() - self._failed.get((bx, by, part), 0) < FAILED_RETRY:
                return None
            try:
                fresh = await self._fetch_part(bx, by, part)
            except _Skipped:
                return None
            if fresh is None:
                self._failed[(bx, by, part)] = time.time()
                try:
                    fresh = await asyncio.to_thread(_read, path)  # an old copy beats none
                except FileNotFoundError:
                    return None
            cached = fresh
        return cached["layers"]

    async def _fetch_part(self, bx: int, by: int, part: str) -> dict | None:
        bounds = block_bounds(bx, by)
        size = bounds[2] - bounds[0]
        pad = size * POINT_BUFFER / EXTENT
        bbox = (bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad)
        keys = PARTS[part]
        built: dict[str, list] = {}
        missing: list[str] = []
        if not near_finland(bounds):
            built = {key: [] for key in keys}  # nothing to fetch out there
        async with self._limit:
            if not built and time.monotonic() - self._wanted.get((bx, by), time.monotonic()) > FORGET_AFTER:
                raise _Skipped  # the map moved on while this block waited its turn
            started = time.monotonic()
            for key in keys if not built else ():
                try:
                    features = await self.fetch_layer(key, bbox)
                    self.errors.pop(key, None)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as err:
                    msg = str(err) or type(err).__name__
                    if self.errors.get(key) != msg:
                        _LOGGER.warning("Chart data %s for block %s,%s failed: %s", key, bx, by, msg)
                    self.errors[key] = msg
                    if key == "depth":
                        return None  # without depths there's no chart to draw
                    features = []
                    missing.append(key)
                if features and key not in self._logged_props:
                    # Logged once, to show which fields a service actually returns.
                    self._logged_props.add(key)
                    _LOGGER.info("Chart data %s fields: %s", key, ", ".join(sorted(features[0].get("properties") or {})))
                # One layer at a time, so only one layer's raw data is ever in memory.
                built.update(await asyncio.to_thread(build_block, bx, by, {key: features}))
                del features
            if part and len(missing) == len(keys):
                return None  # nothing of this part came through; tried again later
        data = {"v": 1, "missing": missing, "layers": built}
        await asyncio.to_thread(_write, self._path(bx, by, part), data)
        self.blocks_fetched += 1
        _LOGGER.info("Chart block %s,%s%s: %s in %.1f s", bx, by, f" ({part})" if part else "",
                     ", ".join(f"{len(v)} {k}" for k, v in built.items()), time.monotonic() - started)
        return data

    async def _fetch_layer(self, key: str, bbox: tuple[float, float, float, float]) -> list[dict]:
        if SOURCES[key][0] == MML_API:
            return await self._fetch_mml(key, bbox)
        return await self._fetch_wfs(key, bbox)

    async def _get_json(self, url: str, params: dict[str, str] | None, what: str) -> dict:
        async with self.session.get(url, params=params, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=120)) as resp:
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise ValueError(f"more than {MAX_RESPONSE // 1048576} MB of {what} data for one area")
                chunks.append(chunk)
            body = b"".join(chunks)
            del chunks
            if resp.status != 200 or not body.lstrip().startswith(b"{"):
                raise aiohttp.ClientError(
                    f"HTTP {resp.status}: {body[:200].decode('utf-8', 'replace').strip()}")
        return await asyncio.to_thread(json.loads, body)  # off the event loop

    async def _fetch_wfs(self, key: str, bbox: tuple[float, float, float, float]) -> list[dict]:
        url, type_name, _, props = SOURCES[key]
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": type_name,
            "srsName": "EPSG:3857", "outputFormat": "application/json", "count": str(PAGE),
            "bbox": ",".join(f"{v:.1f}" for v in bbox) + ",EPSG:3857",
        }
        if url == VAYLA_WFS:
            params["propertyName"] = ",".join((*props, "geom"))
        features: list[dict] = []
        start = 0
        while True:
            params["startIndex"] = str(start)
            page = await self._get_json(url, params, key)
            got = page.get("features") or []
            features += got
            total = page.get("numberMatched", page.get("totalFeatures"))
            if isinstance(total, int):
                if not got or len(features) >= total:
                    return features
            elif len(got) < min(PAGE, 1000):
                return features  # a server cap below PAGE still pages on
            start += len(got)

    async def _fetch_mml(self, key: str, bbox: tuple[float, float, float, float]) -> list[dict]:
        """An MML OGC API Features collection in a bbox, following its next-page links."""
        _, collection, _, _ = SOURCES[key]
        lon0, lat0 = mercator_to_lonlat(bbox[0], bbox[1])
        lon1, lat1 = mercator_to_lonlat(bbox[2], bbox[3])
        url: str | None = f"{MML_API}/collections/{collection}/items"
        params: dict[str, str] | None = {"bbox": f"{lon0:.6f},{lat0:.6f},{lon1:.6f},{lat1:.6f}",
                                         "limit": "10000", "api-key": self.mml_key}
        features: list[dict] = []
        for _ in range(50):
            page = await self._get_json(url, params, key)
            features += await asyncio.to_thread(_mml_features, page)
            nxt = next((link.get("href") for link in page.get("links") or []
                        if link.get("rel") == "next" and link.get("href")), None)
            if not nxt or not page.get("features"):
                break
            url, params = nxt, None
            if "api-key=" not in url:
                url += ("&" if "?" in url else "?") + f"api-key={self.mml_key}"
        return features

    def prune(self) -> None:
        """Drop blocks nobody has looked at for two refresh periods."""
        root = self.dir / "blocks"
        if not root.exists():
            return
        for p in root.glob("*.json.gz"):
            try:
                if time.time() - p.stat().st_mtime > 2 * MAX_AGE:
                    p.unlink()
            except FileNotFoundError:
                pass


def _read(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)
