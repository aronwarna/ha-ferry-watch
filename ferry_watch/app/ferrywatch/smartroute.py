"""Automatic route planning over open water, for the route tool's "Automatic" mode.
Planning only, not for navigation.

The drawn chart's depth areas between the two ends become a grid of cells (40 m across for
a short trip, wider for a long one): water deep enough for the boat, water that may be
(the 0-3 m band tells a small boat nothing more), and everything else. Rocks, wrecks and
shallow soundings close their cells, the shore is kept at a distance, and the marked
fairways deep enough for the boat are drawn in on top, so a narrow sound with a fairway
through it stays open however coarse the grid. Speed limit areas slow their cells.

A motor boat gets the quickest way through that grid (A*): cruising speed, the speed
limits, and time counted longer near the shore and in water of uncertain depth, a little
shorter on a fairway.

A sailing boat gets weather routing: the same search, but each step takes the time the
boat's polar gives for its angle to the forecast wind at that place and hour (tacking and
gybing as speed made good, the engine in calms and where there is no room to tack). Two
routes come back: the fastest, and a comfortable one that counts beating, strong wind,
waves and running dead downwind as longer than they take (see boats.py).

Only chart blocks on disk count; missing ones are downloaded in the background. The grid
is kept a few minutes for re-planning (about a megabyte and a half at most).
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import math
import time
from array import array
from functools import lru_cache
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import boats
from .autoroute import DRAUGHT_MARGIN
from .geo import distance_m
from .vchart import (BLOCK_Z, HALF, UNITS, _depth_band, _num, block_bounds, in_thread, lonlat_to_mercator,
                     mercator_to_lonlat, ne_base, near_finland, read_block)

_LOGGER = logging.getLogger(__name__)

NM = 1852.0
KN = boats.KN
CELL_STEPS = (40, 60, 80, 120, 160, 240, 400)  # ground metres across a cell: the finest that fits
MAX_CELLS = 1_200_000
MAX_CELLS_SAIL = 500_000  # weather routing looks at each cell in 16 directions: a coarser grid
MAX_BLOCKS = 1200  # chart blocks read for one plan (about 340 x 340 km)
PAD_MIN_M, PAD_SHARE, PAD_MAX_M = 6000, 0.25, 25000  # room round the two ends for detours
LEVEL = 1  # the blocks' level of detail read (to the metre; the full one is slower)
MARGINAL_SHARE = 0.5  # a depth band counts as "may be deep enough" if the boat needs less than this far into it
ROCK_CLEAR_M = 30  # rocks, wrecks and shallow soundings close the cells this near
SHORE_CLEAR_M = 30  # the shore and shoals are kept this far off (in whole cells; none on a coarse grid)
NEAR_SHORE_M = 100  # nearer the shore than this counts as longer...
NEAR_SHORE_COST = 1.25
MARGINAL_COST = 3.0  # ...and so does water of uncertain depth
FAIRWAY_COST = 0.85  # a marked fairway counts shorter: once on one, the route stays on it
FAIRWAY_BEND_CELLS = 3.5  # ...and onto the fairway's own turn when one is this near
FAIRWAY_SNAP_CELLS = 1.5  # a turning point on a fairway cell is moved onto the fairway's own line this near
# Shelter: open water counts longer for a small boat, the more the more wind there is.
SHELTER_PX_M = 250  # the land picture the shelter is read from, metres across a dot
SHELTER_RADIUS_M = 1300  # land and shoals about this near give shelter (a soft edge)
SHELTER_UPWIND_M = 1500  # with a forecast: land this far upwind counts most
SHELTER_UPWIND_SHARE = 1.0
SHELTER_LAND_SHARE = 0.10  # this much land around (or upwind): fully sheltered
SHELTER_WIND = (0.35, 2.0)  # the boat's weight times wind / its "okay" wind, kept within these
EXPOSED_FROM = 0.5  # of full exposure: counted as open water in the sums
SHORE_ROOM = {"close": 0.5, "normal": 1.0, "wide": 2.0}  # the panel's "Room from shore": times the shore distances
CHECK_MIN_M = 80  # a stretch near the shore or over uncertain depth this long is listed as a place to check
CHECK_MAX = 12
STRAIGHT_SLACK = 1.02  # a straight line may cost this much more than the way it replaces
FAIRWAY_KEEP = 0.9  # a straight line keeps this much of the fairway the way it replaces ran on
MERGE_M = 600  # two turning points this near are made one where that costs no more than DROP_SLACK
DROP_SLACK = 1.03  # ...and a turning point goes when the line past it costs no more than this
SNAP_MAX_M = 2500  # the start and end join the nearest open water this near
SNAP_WARN_M = 300
MAX_POINTS = 160  # in the answer
CACHE_SECONDS = 600
CACHE_KEEP = 2
BLOCK_WAIT = 8
WIND_GRID = (6, 5)  # forecast points across the area (columns, rows) for weather routing
SAIL_WEIGHT = 1.1  # the search may be this much off the best, for speed
# Cell flags.
NAV, MARGINAL, FAIRWAY, NEAR, NARROW, TRUNK = 1, 2, 4, 8, 16, 32  # TRUNK: the green small-craft route
BORDER = 2  # closed cells round the grid, so the search needs no edge checks

# 16 directions: (rows down, columns right, cells passed through first)
_DIRS = [(-1, 0, ()), (-2, 1, ((-1, 0), (-1, 1))), (-1, 1, ((-1, 0), (0, 1))), (-1, 2, ((0, 1), (-1, 1))),
         (0, 1, ()), (1, 2, ((0, 1), (1, 1))), (1, 1, ((1, 0), (0, 1))), (2, 1, ((1, 0), (1, 1))),
         (1, 0, ()), (2, -1, ((1, 0), (1, -1))), (1, -1, ((1, 0), (0, -1))), (1, -2, ((0, -1), (1, -1))),
         (0, -1, ()), (-1, -2, ((0, -1), (-1, -1))), (-1, -1, ((-1, 0), (0, -1))), (-2, -1, ((-1, 0), (-1, -1)))]
_BEARING = [math.degrees(math.atan2(dc, -dr)) % 360 for dr, dc, _ in _DIRS]
TWS_BINS, DIR_BINS = 26, 72  # wind speed in whole m/s (0-25), direction in 5 degree steps


class Grid:
    """The water between two places: a flag byte and a speed limit (knots, 0: none) per cell."""

    def __init__(self, bx0: int, by0: int, bx1: int, by1: int, cell_m: float):
        self.blocks = (bx0, by0, bx1, by1)
        size = 2 * HALF / 2**BLOCK_Z
        self.x0 = -HALF + bx0 * size
        self.y0 = HALF - by0 * size
        lat_mid = mercator_to_lonlat(0, self.y0 - (by1 - by0 + 1) * size / 2)[1]
        self.k = math.cos(math.radians(lat_mid))  # ground metres per mercator metre
        self.cell_m = cell_m
        self.c = cell_m / self.k
        self.w = round((bx1 - bx0 + 1) * size / self.c) + 2 * BORDER
        self.h = round((by1 - by0 + 1) * size / self.c) + 2 * BORDER
        self.flags = b""
        self.limit = b""
        self.land: Image.Image | None = None  # the share of land and shoals around, coarse (see finish)
        self.fw_lines: dict[tuple[int, int], array] = {}  # fairway lines by 16-cell tile: r0, c0, r1, c1...
        self.fw_bends: dict[tuple[int, int], list[tuple[float, float]]] = {}  # where fairways turn
        self.room = 1.0
        self.built = 0.0
        self.missing = 0
        self.coarse = 0  # blocks with only the base map's coastline
        self.limits_known = False

    def cell(self, lat: float, lon: float) -> tuple[int, int]:
        x, y = lonlat_to_mercator(lon, lat)
        return int((self.y0 - y) / self.c) + BORDER, int((x - self.x0) / self.c) + BORDER

    def latlon(self, r: float, c: float) -> tuple[float, float]:
        lon, lat = mercator_to_lonlat(self.x0 + (c - BORDER + 0.5) * self.c, self.y0 - (r - BORDER + 0.5) * self.c)
        return lat, lon

    def inside(self, r: int, c: int) -> bool:
        return BORDER <= r < self.h - BORDER and BORDER <= c < self.w - BORDER

    def block_px(self, bx: int, by: int) -> tuple[int, int, int, int]:
        """A block's cells in the grid: (left, top, right, bottom), each cell in one block."""
        size = 2 * HALF / 2**BLOCK_Z / self.c
        i, j = bx - self.blocks[0], by - self.blocks[1]
        return (round(i * size) + BORDER, round(j * size) + BORDER, round((i + 1) * size) + BORDER,
                round((j + 1) * size) + BORDER)

    def nbytes(self) -> int:
        return len(self.flags) + len(self.limit)


# --- a block's cells -----------------------------------------------------------------------

def depth_class(drval1: Any, drval2: Any, need: float) -> int:
    """0: too shallow, 1: may be deep enough, 2: deep enough, for a boat that needs `need` m."""
    d1, d2 = _num(drval1), _num(drval2)
    if d1 is None:
        d1 = 0.0 if _depth_band(drval1, drval2) <= 1 else 10.0
    if d1 >= need:
        return 2
    if d1 < 0 or d2 is None or d2 <= d1:
        return 0
    return 1 if need <= d1 + MARGINAL_SHARE * (d2 - d1) else 0


def render_block(layers: dict[str, Any], w: int, h: int, need: float, draught: float, cell_m: float,
                 sea: list | None = None) -> tuple[Image.Image, Image.Image, list[list[tuple[float, float]]]]:
    """A block's cells from its chart layers: (depth class per cell, fairway mask, the
    fairway lines in cells)."""
    lines: list[list[tuple[float, float]]] = []
    img = Image.new("L", (w, h), 0)
    fw = Image.new("L", (w, h), 0)
    draw, fdraw = ImageDraw.Draw(img), ImageDraw.Draw(fw)
    sx, sy = w / UNITS, h / UNITS

    def ring(layer: Any, r: int) -> list[tuple[float, float]]:
        co = layer.co
        return [(co[2 * v] * sx, co[2 * v + 1] * sy) for v in range(layer.vo[r], layer.vo[r + 1])]

    for x0, y0, x1, y1 in sea or ():  # open sea Traficom's depth areas stop short of
        draw.rectangle((x0 * sx, y0 * sy, x1 * sx, y1 * sy), fill=2)
    xdepth = layers.get("xdepth")
    if xdepth is not None and xdepth.n:  # foreign water, nested: deeper than 0, 3, 6, 10 m
        steps = (0, 3, 6, 10, 20)
        rows = sorted(range(xdepth.n), key=lambda i: _num(xdepth.props[xdepth.pi[i]][0]) or 0)
        for i in rows:
            level = _num(xdepth.props[xdepth.pi[i]][0]) or 0
            deeper = next((s for s in steps if s > level), level + 10)
            value = depth_class(level, deeper, need)
            rings = [ring(xdepth, r) for r in xdepth.rings(i)]
            if not rings or len(rings[0]) < 3:
                continue
            mask = Image.new("1", (w, h), 0)
            mdraw = ImageDraw.Draw(mask)
            mdraw.polygon(rings[0], fill=1)
            for hole in rings[1:]:
                if len(hole) >= 3:
                    mdraw.polygon(hole, fill=0)
            img.paste(value, (0, 0), mask)
    depth = layers.get("depth")
    if depth is not None and depth.n:
        def box_area(i: int) -> int:
            x0, y0, x1, y1 = depth.bbox(i)
            return (x1 - x0) * (y1 - y0)
        # Biggest first: a hole is emptied, then the smaller areas lying in it are drawn.
        for i in sorted(range(depth.n), key=box_area, reverse=True):
            props = depth.props[depth.pi[i]]
            value = depth_class(props[0] if props else None, props[1] if props and len(props) > 1 else None, need)
            for n, r in enumerate(depth.rings(i)):
                pts = ring(depth, r)
                if len(pts) >= 3:
                    draw.polygon(pts, fill=0 if n else value)
    rockarea = layers.get("rockarea")
    for i in range(rockarea.n if rockarea is not None else 0):
        pts = ring(rockarea, rockarea.rings(i)[0])
        if len(pts) >= 3:
            draw.polygon(pts, fill=0)
    rx, ry = max(0.5, ROCK_CLEAR_M / cell_m), max(0.5, ROCK_CLEAR_M / cell_m)

    def close(layer: Any, i: int) -> None:
        x, y = layer.co[2 * i] * sx, layer.co[2 * i + 1] * sy
        if -rx <= x <= w + rx and -ry <= y <= h + ry:
            draw.ellipse((x - rx, y - ry, x + rx, y + ry), fill=0)

    for key in ("rock", "wreck"):
        layer = layers.get(key)
        for i in range(layer.n if layer is not None else 0):
            close(layer, i)
    trock = layers.get("trock")
    for i in range(trock.n if trock is not None else 0):
        props = trock.props[trock.pi[i]]
        least = _num(props[2]) if len(props) > 2 else None
        if least is None or least < need:
            close(trock, i)
    sounding = layers.get("sounding")
    for i in range(sounding.n if sounding is not None else 0):
        d = _num(sounding.props[sounding.pi[i]][0])
        if d is not None and d < need:
            close(sounding, i)
    fline = layers.get("fline")
    for i in range(fline.n if fline is not None else 0):
        props = fline.props[fline.pi[i]]
        design = _num(props[0]) if props else None
        if design is not None and design < draught + DRAUGHT_MARGIN:
            continue
        co = fline.co
        pts = [(co[2 * v] * sx, co[2 * v + 1] * sy) for v in range(fline.vo[i], fline.vo[i + 1])]
        if len(pts) >= 2:
            fdraw.line(pts, fill=128, width=1)
            lines.append(pts)
    # The small-craft trunk routes (the chart's green band): 255 in the fairway picture.
    route = layers.get("route")
    for i in range(route.n if route is not None else 0):
        props = route.props[route.pi[i]]
        if not props or _num(props[0]) != 4:
            continue
        deep = _num(props[2]) if len(props) > 2 else None
        if deep is not None and deep < draught + DRAUGHT_MARGIN:
            continue
        co = route.co
        pts = [(co[2 * v] * sx, co[2 * v + 1] * sy) for v in range(route.vo[i], route.vo[i + 1])]
        if len(pts) >= 2:
            fdraw.line(pts, fill=255, width=1)
            lines.append(pts)
    return img, fw, lines


def render_base(bx: int, by: int, w: int, h: int) -> Image.Image:
    """A block with no chart data: the base map's sea, less its land (a coarse coastline)."""
    minx, miny, maxx, maxy = block_bounds(bx, by)
    img = Image.new("L", (w, h), 2)
    draw = ImageDraw.Draw(img)
    sx, sy = w / (maxx - minx), h / (maxy - miny)
    for kind in ("land",):
        for (px0, py0, px1, py1), rings in ne_base()[kind]:
            if px1 < minx or px0 > maxx or py1 < miny or py0 > maxy:
                continue
            for n, co in enumerate(rings):
                pts = [((co[i] - minx) * sx, (maxy - co[i + 1]) * sy) for i in range(0, len(co), 2)]
                if len(pts) >= 3:
                    draw.polygon(pts, fill=2 if n else 0)
    return img


def finish(grid: Grid, cls: Image.Image, fw: Image.Image, areas: list[dict[str, Any]] | None = None,
           room: float = 1.0) -> None:
    """The grid's flags from the depth classes and the fairway mask, and its speed limits.
    `room` scales the distances kept from the shore and shoals (the panel's "Room from shore")."""
    cell = grid.cell_m
    grid.room = room
    trunk = fw.point(lambda v: 255 if v == 255 else 0)
    fw = fw.point(lambda v: 255 if v else 0)
    nav = cls.point(lambda v: 255 if v else 0)
    marginal = cls.point(lambda v: 255 if v == 1 else 0)
    shore = round(SHORE_CLEAR_M * room / cell)
    if shore:
        nav = nav.filter(ImageFilter.MinFilter(2 * shore + 1))
    nav = ImageChops.lighter(nav, fw)
    marginal = ImageChops.multiply(marginal, ImageChops.invert(fw))  # a fairway is deep enough
    near = max(1, math.ceil(NEAR_SHORE_M * room / cell))
    # Near the shore, and as near water of uncertain depth: shoals are given room too.
    sure = ImageChops.multiply(nav, ImageChops.invert(marginal))
    open_water = sure.filter(ImageFilter.MinFilter(2 * near + 1))
    narrow = max(1, math.ceil(boats.SAIL["narrow_m"] / cell))
    wide = nav.filter(ImageFilter.MinFilter(2 * narrow + 1))
    flags = nav.point(lambda v: NAV if v else 0)
    # Shelter: how much land and water too shallow for waves to run lies around each place.
    f = max(1, round(SHELTER_PX_M / cell))
    land = cls.point(lambda v: 0 if v else 255)
    small = land.reduce(f) if f > 1 else land
    grid.land = small.filter(ImageFilter.GaussianBlur(SHELTER_RADIUS_M / (cell * f)))
    for mask, bit in ((marginal, MARGINAL), (fw, FAIRWAY), (ImageChops.invert(open_water), NEAR),
                      (ImageChops.invert(wide), NARROW), (trunk, TRUNK)):
        flags = ImageChops.add(flags, ImageChops.darker(mask, nav).point(lambda v, bit=bit: bit if v else 0))
    # The closed border.
    framed = Image.new("L", flags.size, 0)
    inner = (BORDER, BORDER, grid.w - BORDER, grid.h - BORDER)
    framed.paste(flags.crop(inner), inner)
    grid.flags = framed.tobytes()
    lim = Image.new("L", flags.size, 0)
    if areas:
        draw = ImageDraw.Draw(lim)
        for area in sorted(areas, key=lambda a: -a["kmh"]):  # the lowest limit drawn last
            pts = []
            for lat, lon in area["ring"]:
                x, y = lonlat_to_mercator(lon, lat)
                pts.append(((x - grid.x0) / grid.c + BORDER, (grid.y0 - y) / grid.c + BORDER))
            if len(pts) >= 3:
                draw.polygon(pts, fill=max(1, min(255, round(area["kmh"] / 1.852))))
    grid.limit = lim.tobytes()


def exposure(grid: Grid, wind_from: float | None = None) -> bytes:
    """How open each cell lies, 0 (among islands) to 255 (open sea). With a wind direction the
    land upwind counts most: the lee of an island is sheltered, its windward side is not."""
    land = grid.land
    if land is None:
        return bytes(grid.w * grid.h)
    share = land
    if wind_from is not None:
        px = grid.cell_m * grid.w / land.size[0]
        d = SHELTER_UPWIND_M / px
        a = math.radians(wind_from)
        upwind = land.transform(land.size, Image.Transform.AFFINE, (1, 0, d * math.sin(a), 0, 1, -d * math.cos(a)),
                                Image.Resampling.BILINEAR)
        share = Image.blend(land, upwind, SHELTER_UPWIND_SHARE)
    full = 255 * SHELTER_LAND_SHARE
    expo = share.point(lambda v: max(0, round(255 * (1 - v / full))))
    return expo.resize((grid.w, grid.h), Image.Resampling.BILINEAR).tobytes()


def add_fairway_lines(grid: Grid, lines: list[list[tuple[float, float]]], left: int, top: int) -> None:
    """Keeps a block's fairway lines (in its own cells) for moving turning points onto them."""
    store = grid.fw_lines
    for pts in lines:
        for (x0, y0), (x1, y1), (x2, y2) in zip(pts, pts[1:], pts[2:]):  # where the fairway itself turns
            turn = abs((math.degrees(math.atan2(y2 - y1, x2 - x1) - math.atan2(y1 - y0, x1 - x0)) + 180) % 360 - 180)
            if turn >= 5:
                grid.fw_bends.setdefault((int(top + y1) >> 4, int(left + x1) >> 4), []).append((top + y1, left + x1))
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            n = max(1, math.ceil(max(abs(x1 - x0), abs(y1 - y0)) / 12))  # pieces of at most 12 cells
            for i in range(n):
                ra, ca = top + y0 + (y1 - y0) * i / n, left + x0 + (x1 - x0) * i / n
                rb, cb = top + y0 + (y1 - y0) * (i + 1) / n, left + x0 + (x1 - x0) * (i + 1) / n
                key = (int((ra + rb) / 2) >> 4, int((ca + cb) / 2) >> 4)
                bucket = store.get(key)
                if bucket is None:
                    bucket = store[key] = array("f")
                bucket.extend((ra, ca, rb, cb))


def on_fairway(grid: Grid, r: int, c: int) -> tuple[float, float] | None:
    """The point of a fairway's own line nearest a cell's middle, within FAIRWAY_SNAP_CELLS."""
    pr, pc = r + 0.5, c + 0.5
    # A turn of the route by a turn of the fairway is that turn.
    best, at = FAIRWAY_BEND_CELLS**2, None
    for tr in range((r >> 4) - 1, (r >> 4) + 2):
        for tc in range((c >> 4) - 1, (c >> 4) + 2):
            for qr, qc in grid.fw_bends.get((tr, tc), ()):
                d = (qr - pr) ** 2 + (qc - pc) ** 2
                if d < best:
                    best, at = d, (qr - 0.5, qc - 0.5)
    if at is not None:
        return at
    best, at = FAIRWAY_SNAP_CELLS**2, None  # the lines are in cell edges' numbers: a cell's middle is half a cell on
    for tr in range((r >> 4) - 1, (r >> 4) + 2):
        for tc in range((c >> 4) - 1, (c >> 4) + 2):
            seg = grid.fw_lines.get((tr, tc))
            for i in range(0, len(seg) if seg else 0, 4):
                r0, c0, r1, c1 = seg[i], seg[i + 1], seg[i + 2], seg[i + 3]
                dr, dc = r1 - r0, c1 - c0
                ll = dr * dr + dc * dc
                f = 0.0 if ll <= 0 else min(1.0, max(0.0, ((pr - r0) * dr + (pc - c0) * dc) / ll))
                qr, qc = r0 + dr * f, c0 + dc * f
                d = (qr - pr) ** 2 + (qc - pc) ** 2
                if d < best:
                    best, at = d, (qr - 0.5, qc - 0.5)
    return at


# --- the search ----------------------------------------------------------------------------

def nearest_open(grid: Grid, r: int, c: int) -> tuple[int, int] | None:
    """The open cell nearest a cell, within SNAP_MAX_M."""
    flags, w = grid.flags, grid.w
    if grid.inside(r, c) and flags[r * w + c]:
        return r, c
    best = None
    for ring in range(1, int(SNAP_MAX_M / grid.cell_m) + 2):
        if best is not None and ring > best[0] * 1.42 + 1:
            break
        for dr in range(-ring, ring + 1):
            cols = (-ring, ring) if abs(dr) < ring else range(-ring, ring + 1)
            for dc in cols:
                rr, cc = r + dr, c + dc
                if grid.inside(rr, cc) and flags[rr * w + cc]:
                    d = math.hypot(dr, dc)
                    if best is None or d < best[0]:
                        best = (d, rr, cc)
    return (best[1], best[2]) if best else None


def _cost_table(fairway: float = FAIRWAY_COST, trunk: float | None = None) -> list[float]:
    """How much longer (or shorter) a cell counts by its flags. `fairway` and `trunk` are the
    boat's own: a small boat counts fairways, and most of all the green small-craft routes,
    much shorter than open water."""
    trunk = fairway if trunk is None else trunk
    out = []
    for f in range(64):
        m = 1.0
        if f & MARGINAL:
            m *= MARGINAL_COST
        if f & NEAR and not f & FAIRWAY:
            m *= NEAR_SHORE_COST
        if f & TRUNK:
            m *= trunk
        elif f & FAIRWAY:
            m *= fairway
        out.append(m)
    return out


def exposure_table(weight: float) -> list[float]:
    return [1 + weight * i / 255 for i in range(256)]


def search_power(grid: Grid, start: tuple[int, int], end: tuple[int, int], speed_kn: float,
                 expo: bytes | None = None, weight: float = 0.0,
                 fairway: tuple[float, float] = (FAIRWAY_COST, FAIRWAY_COST)) -> dict[str, Any] | None:
    """The quickest way for a motor boat: {"cells": [(r, c)...], "seconds", "cost": per cell}."""
    flags, limit, w = grid.flags, grid.limit, grid.w
    mult = _cost_table(*fairway)
    v = max(0.5, speed_kn) * KN
    has_limits = any(limit) if limit else False
    lim_v = [v] + [min(v, kn * KN) for kn in range(1, 256)]
    cell = grid.cell_m
    if not expo or weight <= 0:
        expo, weight = bytes(len(flags)), 0.0
    ex = exposure_table(weight)
    moves = []
    for d in (0, 2, 4, 6, 8, 10, 12, 14):
        dr, dc, through = _DIRS[d]
        moves.append((dr * w + dc, math.hypot(dr, dc) * cell, [a * w + b for a, b in through]))
    s, e = start[0] * w + start[1], end[0] * w + end[1]
    er, ec = end
    inf = math.inf
    g = array("d", [inf]) * len(flags)
    t = array("d", [0.0]) * len(flags)
    prev = array("i", [-1]) * len(flags)
    hk = cell / v * min(mult)
    g[s] = 0.0
    heap = [(math.hypot(start[0] - er, start[1] - ec) * hk, 0.0, s)]
    push, pop, hypot = heapq.heappush, heapq.heappop, math.hypot
    while heap:
        _, cost, u = pop(heap)
        if u == e:
            break
        if cost > g[u]:
            continue
        tu = t[u]
        for off, dist, through in moves:
            n = u + off
            f = flags[n]
            if not f:
                continue
            if through and not (flags[u + through[0]] and flags[u + through[1]]):
                continue
            step = dist / (lim_v[limit[n]] if has_limits else v)
            nc = cost + step * mult[f] * ex[expo[n]]
            if nc < g[n]:
                g[n] = nc
                t[n] = tu + step
                prev[n] = u
                push(heap, (nc + hypot(n // w - er, n % w - ec) * hk, nc, n))
    if g[e] == inf:
        return None
    cells = [e]
    while cells[-1] != s:
        cells.append(prev[cells[-1]])
    cells.reverse()
    return {"cells": [(i // w, i % w) for i in cells], "seconds": t[e], "g": [g[i] for i in cells],
            "t": [t[i] for i in cells], "expo": expo, "weight": weight, "fairway": fairway}


@lru_cache(maxsize=1)
def sail_tables() -> tuple[list[float], list[str], list[float], list[float]]:
    """Per wind speed bin, wind direction bin, heading and room to tack: the speed made good
    (m/s), how (s sail, b beat, g gybe, m motor), the comfort factor without waves, and the
    speed the search counts with. That last one is the beating speed where the engine is on
    only because there is no room to tack: otherwise the search would hug the shore to motor
    upwind instead of sailing."""
    speed, how, comfort, counted = [], [], [], []
    for tb in range(TWS_BINS):
        for db in range(DIR_BINS):
            wind_from = db * 360 / DIR_BINS
            for hd in range(16):
                twa = abs((wind_from - _BEARING[hd] + 180) % 360 - 180)
                open_kn, open_how = boats.made_good(float(tb), twa, False)
                for narrow in (False, True):
                    kn, h = boats.made_good(float(tb), twa, narrow)
                    speed.append(kn * KN)
                    how.append(h[0])
                    comfort.append(boats.comfort_factor(open_how if narrow else h, twa, float(tb), None))
                    counted.append(min(kn, open_kn) * KN)
    return speed, how, comfort, counted


class Wind:
    """The forecast over the grid for weather routing: per hour and forecast point the wind's
    speed and direction bins and the waves, and which forecast point each part of the grid uses."""

    def __init__(self, grid: Grid, pts: list[tuple[float, float, float]], air: Any, sea: Any, shape: tuple[int, int]):
        from .dayscore import shelter
        self.times = list(air.times)
        self.t0 = self.times[0] if self.times else 0
        self.hours = len(self.times)
        n = len(pts)
        self.n = n
        self.tb: list[int] = []
        self.db: list[int] = []
        self.wave: list[float] = []
        cell_km = (sea.extra.get("cell_km") if sea is not None else None) or [None] * n
        for j, ts in enumerate(self.times):
            for i in range(n):
                ms = air.values["ms"][i][j]
                d = air.values["dir"][i][j] if "dir" in air.values else None
                self.tb.append(min(TWS_BINS - 1, max(0, round(ms if ms is not None else 0))))
                self.db.append(int(round((d or 0) / (360 / DIR_BINS))) % DIR_BINS)
                wv = sea.at("wave", i, ts) if sea is not None else None
                self.wave.append((wv or 0.0) * shelter(cell_km[i] if i < len(cell_km) else None))
        # Which forecast point a cell uses: by 16 x 16 cell tiles, the nearest.
        self.shift = 4
        self.tw = (grid.w >> self.shift) + 1
        th = (grid.h >> self.shift) + 1
        cols, rows = shape
        self.node = []
        for tr in range(th):
            fr = min(rows - 1, max(0, round(((tr << self.shift) + 8 - BORDER) / max(1, grid.h - 2 * BORDER) * rows - 0.5)))
            for tc in range(self.tw):
                fc = min(cols - 1, max(0, round(((tc << self.shift) + 8 - BORDER) / max(1, grid.w - 2 * BORDER) * cols - 0.5)))
                self.node.append(fr * cols + fc)


def wind_points(grid: Grid) -> tuple[list[tuple[float, float, float]], tuple[int, int]]:
    """Forecast points spread over the grid, row by row from the top: (lat, lon, 0)."""
    cols, rows = WIND_GRID
    w, h = grid.w - 2 * BORDER, grid.h - 2 * BORDER
    # No closer together than about 8 km: a small area needs fewer.
    cols = max(1, min(cols, round(w * grid.cell_m / 8000)))
    rows = max(1, min(rows, round(h * grid.cell_m / 8000)))
    pts = []
    for r in range(rows):
        for c in range(cols):
            lat, lon = grid.latlon(BORDER + (r + 0.5) * h / rows, BORDER + (c + 0.5) * w / cols)
            pts.append((round(lat, 4), round(lon, 4), 0.0))
    return pts, (cols, rows)


def search_sail(grid: Grid, start: tuple[int, int], end: tuple[int, int], wind: Wind, depart: float,
                comfortable: bool) -> dict[str, Any] | None:
    """Weather routing for the sailing boat (see the module docstring)."""
    flags, limit, w = grid.flags, grid.limit, grid.w
    speed, how, comfort, counted = sail_tables()
    s_ = boats.SAIL
    cell = grid.cell_m
    moves = [(dr * w + dc, math.hypot(dr, dc) * cell, [a * w + b for a, b in through], hd * 2)
             for hd, (dr, dc, through) in enumerate(_DIRS)]
    s, e = start[0] * w + start[1], end[0] * w + end[1]
    er, ec = end
    inf = math.inf
    g = array("d", [inf]) * len(flags)
    t = array("d", [0.0]) * len(flags)
    prev = array("i", [-1]) * len(flags)
    hows = bytearray(len(flags))
    vmax = max(speed) or s_["motor_kn"] * KN
    hk = cell / vmax * SAIL_WEIGHT
    g[s] = 0.0
    heap = [(math.hypot(start[0] - er, start[1] - ec) * hk, 0.0, s)]
    push, pop, hypot = heapq.heappush, heapq.heappop, math.hypot
    tb_, db_, wave_, node_, tw, shift, nn = wind.tb, wind.db, wind.wave, wind.node, wind.tw, wind.shift, wind.n
    last_hour = wind.hours - 1
    off0 = depart - wind.t0
    marg, near_cost = MARGINAL_COST, NEAR_SHORE_COST
    wave_from, wave_k = s_["comfort_wave_m"], s_["comfort_wave"]
    while heap:
        _, cost, u = pop(heap)
        if u == e:
            break
        if cost > g[u]:
            continue
        tu = t[u]
        hour = int((off0 + tu) / 3600)
        hour = 0 if hour < 0 else last_hour if hour > last_hour else hour
        wi = hour * nn + node_[((u // w) >> shift) * tw + ((u % w) >> shift)]
        base = (tb_[wi] * DIR_BINS + db_[wi]) * 32
        wave = wave_[wi]
        wf = 1 + wave_k * (wave - wave_from) if comfortable and wave > wave_from else 1.0
        for off, dist, through, hd2 in moves:
            n = u + off
            f = flags[n]
            if not f:
                continue
            if not (flags[u + through[0]] and flags[u + through[1]]) if through else False:
                continue
            k = base + hd2 + (1 if f & NARROW else 0)
            v = speed[k]
            lv = limit[n]
            if lv and v > lv * KN:
                v = lv * KN
            step = dist / v
            vc = counted[k]
            m = v / vc if vc < v else 1.0
            if f & MARGINAL:
                m *= marg
            elif f & NEAR and not f & FAIRWAY:
                m *= near_cost
            if comfortable:
                m *= comfort[k] * wf
            nc = cost + step * m
            if nc < g[n]:
                g[n] = nc
                t[n] = tu + step
                prev[n] = u
                hows[n] = ord(how[k])
                push(heap, (nc + hypot(n // w - er, n % w - ec) * hk, nc, n))
    if g[e] == inf:
        return None
    cells = [e]
    while cells[-1] != s:
        cells.append(prev[cells[-1]])
    cells.reverse()
    return {"cells": [(i // w, i % w) for i in cells], "seconds": t[e], "g": [g[i] for i in cells],
            "t": [t[i] for i in cells], "how": [chr(hows[i]) if hows[i] else "s" for i in cells]}


# --- from cells to a route -------------------------------------------------------------------

def _line_cells(a: tuple[float, float], b: tuple[float, float]) -> list[tuple[int, int]]:
    """The cells a straight line passes (sampled every half cell)."""
    n = max(1, int(2 * max(abs(b[0] - a[0]), abs(b[1] - a[1]))))
    out = []
    last = None
    for i in range(n + 1):
        f = i / n
        cell = (int(a[0] + (b[0] - a[0]) * f + 0.5), int(a[1] + (b[1] - a[1]) * f + 0.5))
        if cell != last:
            out.append(cell)
            last = cell
    return out


def _line_cost(grid: Grid, a: tuple[int, int], b: tuple[int, int], speed_ms: float, mult: list[float],
              expo: bytes | None = None, ex: list[float] | None = None) -> float:
    """What a straight line between two cells costs (as search_power counts), inf if closed."""
    flags, limit, w = grid.flags, grid.limit, grid.w
    cells = _line_cells(a, b)
    total = 0.0
    for r, c in cells:
        i = r * w + c
        f = flags[i]
        if not f:
            return math.inf
        if not f & FAIRWAY:
            # A straight line and the fairway's drawn line differ by a cell here and there:
            # beside a fairway cell counts as on it.
            f |= (flags[i - 1] | flags[i + 1] | flags[i - w] | flags[i + w]) & (FAIRWAY | TRUNK)
        lv = limit[i]
        m = mult[f] * ex[expo[i]] if expo else mult[f]
        total += m / (min(speed_ms, lv * KN) if lv else speed_ms)
    return total / len(cells) * math.hypot(b[0] - a[0], b[1] - a[1]) * grid.cell_m


def straighten(grid: Grid, cells: list[tuple[int, int]], g: list[float], speed_ms: float,
               expo: bytes | None = None, weight: float = 0.0,
               fairway: tuple[float, float] = (FAIRWAY_COST, FAIRWAY_COST)) -> list[int]:
    """Indices of the cells kept when every stretch that a straight line does as well is
    replaced by that line: the grid's eight directions leave a staircase otherwise. Then
    turning points the route does as well without are taken out, so no small kinks stay.
    A stretch along a fairway is only replaced by a line that is on the fairway too: the
    route doesn't cut a fairway's bends or run beside it."""
    mult = _cost_table(*fairway)
    ex = exposure_table(weight) if expo and weight > 0 else None
    if ex is None:
        expo = None
    flags, w = grid.flags, grid.w
    on_fw = [0]
    for r, c in cells:
        on_fw.append(on_fw[-1] + (1 if flags[r * w + c] & FAIRWAY else 0))

    def cost(i: int, j: int) -> float:
        """A straight line from cell i to cell j, inf if closed or off a fairway the way keeps to."""
        had = on_fw[j + 1] - on_fw[i]
        if had > 3:
            line = _line_cells(cells[i], cells[j])
            if sum(1 for r, c in line if (flags[r * w + c] | flags[r * w + c - 1] | flags[r * w + c + 1]
                                          | flags[(r - 1) * w + c] | flags[(r + 1) * w + c]) & FAIRWAY) < FAIRWAY_KEEP * had - 2:
                return math.inf
        return _line_cost(grid, cells[i], cells[j], speed_ms, mult, expo, ex)

    keep = [0]
    i, n = 0, len(cells)
    while i < n - 1:
        good, step = i + 1, 2
        while i + step < n:
            j = i + step
            if cost(i, j) <= (g[j] - g[i]) * STRAIGHT_SLACK + 1e-6:
                good = j
                step *= 2
            else:
                break
        lo, hi = good, min(n - 1, i + step)
        while hi - lo > 1:  # the furthest between the last that did and the first that didn't
            mid = (lo + hi) // 2
            if cost(i, mid) <= (g[mid] - g[i]) * STRAIGHT_SLACK + 1e-6:
                lo = mid
            else:
                hi = mid
        if hi == n - 1 and lo != hi and cost(i, hi) <= (g[hi] - g[i]) * STRAIGHT_SLACK + 1e-6:
            lo = hi
        keep.append(lo)
        i = lo
    for _ in range(4):
        changed = False
        # Turning points the route does as well without.
        j = 1
        while j < len(keep) - 1:
            a, m, b = keep[j - 1], keep[j], keep[j + 1]
            if cost(a, b) <= (cost(a, m) + cost(m, b)) * DROP_SLACK + 1e-6:
                del keep[j]
                changed = True
            else:
                j += 1
        # Two turning points close together become one where that does as well: a small
        # step round a shoal turns into a single bend.
        j = 1
        while j < len(keep) - 2:
            a, m1, m2, b = keep[j - 1], keep[j], keep[j + 1], keep[j + 2]
            (r1, c1), (r2, c2) = cells[m1], cells[m2]
            if math.hypot(r2 - r1, c2 - c1) * grid.cell_m <= MERGE_M:
                now = cost(a, m1) + cost(m1, m2) + cost(m2, b)
                best = min(((cost(a, k) + cost(k, b), k) for k in {m1, (m1 + m2) // 2, m2}), key=lambda t: t[0])
                if best[0] <= now * DROP_SLACK + 1e-6:
                    keep[j:j + 2] = [best[1]]
                    changed = True
                    continue
            j += 1
        if not changed:
            break
    return keep


line_cost = _line_cost


def _open_line(grid: Grid, a: tuple[int, int], b: tuple[int, int]) -> bool:
    flags, w = grid.flags, grid.w
    return all(flags[r * w + c] for r, c in _line_cells(a, b))


def thin(grid: Grid, cells: list[tuple[int, int]], tol: float) -> list[int]:
    """Douglas-Peucker over cells (indices kept), never across closed cells."""
    keep = {0, len(cells) - 1}
    stack = [(0, len(cells) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        (r0, c0), (r1, c1) = cells[a], cells[b]
        dr, dc = r1 - r0, c1 - c0
        length = math.hypot(dr, dc) or 1.0
        worst, at = 0.0, -1
        for i in range(a + 1, b):
            d = abs((cells[i][0] - r0) * dc - (cells[i][1] - c0) * dr) / length
            if d > worst:
                worst, at = d, i
        if worst > tol or not _open_line(grid, cells[a], cells[b]):
            if at < 0:
                at = (a + b) // 2
            keep.add(at)
            stack += [(a, at), (at, b)]
    return sorted(keep)


def describe(grid: Grid, cells: list[tuple[int, int]], idx: list[int], found: dict[str, Any], sail: bool,
             start: tuple[float, float], end: tuple[float, float]) -> dict[str, Any]:
    """The answer for the panel: points, how each stretch is travelled, and the sums."""
    flags, w = grid.flags, grid.w
    # A turning point on a fairway goes onto the fairway's own line, not the middle of its cell.
    at = []
    for n, i in enumerate(idx):
        r, c = cells[i]
        near_fw = (flags[r * w + c] | flags[r * w + c - 1] | flags[r * w + c + 1] | flags[(r - 1) * w + c]
                   | flags[(r + 1) * w + c]) & FAIRWAY
        snapped = on_fairway(grid, r, c) if 0 < n < len(idx) - 1 and near_fw else None
        at.append(snapped or (r, c))
    points = [[round(v, 6) for v in grid.latlon(*p)] for p in at]
    hows = []
    sums: dict[str, float] = {}
    shallow = fairway = exposed = trunk = 0.0
    runs: list[list[Any]] = []  # places to check: [kind, metres, cells]
    gap = 99
    expo = found.get("expo") if found.get("weight") else None
    for a, b in zip(idx, idx[1:]):
        on = _line_cells(cells[a], cells[b])
        # Beside a fairway cell counts as on it (the line and the fairway's drawn cells differ by one).
        fl = [flags[r * w + c] | ((flags[r * w + c - 1] | flags[r * w + c + 1] | flags[(r - 1) * w + c]
                                   | flags[(r + 1) * w + c]) & (FAIRWAY | TRUNK)) for r, c in on]
        metres = math.hypot(cells[b][0] - cells[a][0], cells[b][1] - cells[a][1]) * grid.cell_m
        marginal = sum(1 for f in fl if f & MARGINAL) / len(fl)
        on_fw = sum(1 for f in fl if f & FAIRWAY) / len(fl)
        shallow += metres * marginal
        fairway += metres * on_fw
        trunk += metres * sum(1 for f in fl if f & TRUNK) / len(fl)
        step = metres / len(fl)
        for cell, f in zip(on, fl):
            kind = "depth" if f & MARGINAL else "near" if f & NEAR and not f & FAIRWAY else None
            if kind is None:
                gap += 1  # a gap of a cell or two doesn't end a stretch
                continue
            if runs and gap <= 2 and runs[-1][0] == kind:
                runs[-1][1] += step
                runs[-1][2].append(cell)
            else:
                runs.append([kind, step, [cell]])
            gap = 0
        if expo:
            exposed += metres * sum(1 for r, c in on if expo[r * w + c] >= 255 * EXPOSED_FROM) / len(on)
        if sail:
            run = found["how"][a + 1:b + 1] or ["s"]
            code = max(set(run), key=run.count)
        else:
            code = "f" if on_fw >= 0.6 else "o"
        sums[code] = sums.get(code, 0.0) + metres
        hows.append(code.upper() if marginal > 0.15 else code)
    # The real start and end, joined straight to the first and last open cells.
    first, last = [round(start[0], 6), round(start[1], 6)], [round(end[0], 6), round(end[1], 6)]
    d0, d1 = distance_m(*first, *points[0]), distance_m(*last, *points[-1])
    extra = 0.0
    if d0 > grid.cell_m * 0.75:
        points.insert(0, first)
        hows.insert(0, hows[0] if hows else "o")
        extra += d0
    else:
        points[0] = first
    if d1 > grid.cell_m * 0.75:
        points.append(last)
        hows.append(hows[-1] if hows else "o")
        extra += d1
    else:
        points[-1] = last
    metres = sum(distance_m(*a, *b) for a, b in zip(points, points[1:]))
    names = {"o": "open", "f": "fairway", "s": "sail", "b": "beat", "g": "gybe", "m": "motor"}
    checks = [r for r in runs if r[1] >= max(CHECK_MIN_M, 1.5 * grid.cell_m)]
    if len(checks) > CHECK_MAX:  # the longest, still in the order they come
        keep = set(map(id, sorted(checks, key=lambda r: -r[1])[:CHECK_MAX]))
        checks = [r for r in checks if id(r) in keep]
    places = []
    for kind, m, on_cells in checks:
        lat, lon = grid.latlon(*on_cells[len(on_cells) // 2])
        places.append({"kind": kind, "m": round(m, -1), "lat": round(lat, 6), "lon": round(lon, 6)})
    return {"points": points, "hows": "".join(hows), "metres": round(metres), "seconds": round(found["seconds"]),
            "nm": {names[k]: round(v / NM, 1) for k, v in sums.items()},
            "shallow_nm": round(shallow / NM, 1), "fairway_nm": round(fairway / NM, 1), "trunk_nm": round(trunk / NM, 1),
            "exposed_nm": round(exposed / NM, 1) if expo else None,
            "checks": places, "snap_m": [round(d0), round(d1)]}


# --- the planner ---------------------------------------------------------------------------

def corridor(start: tuple[float, float], end: tuple[float, float]) -> tuple[int, int, int, int]:
    """The chart blocks over both ends and the room round them: (bx0, by0, bx1, by1)."""
    pts = [lonlat_to_mercator(lo, la) for la, lo in (start, end)]
    k = math.cos(math.radians((start[0] + end[0]) / 2))
    span = math.hypot(pts[0][0] - pts[1][0], pts[0][1] - pts[1][1])
    pad = max(PAD_MIN_M / k, min(span * PAD_SHARE, PAD_MAX_M / k))
    size = 2 * HALF / 2**BLOCK_Z
    return (math.floor((min(p[0] for p in pts) - pad + HALF) / size),
            math.floor((HALF - max(p[1] for p in pts) - pad) / size),
            math.floor((max(p[0] for p in pts) + pad + HALF) / size),
            math.floor((HALF - min(p[1] for p in pts) + pad) / size))


def cell_size(blocks: tuple[int, int, int, int], lat: float, max_cells: int) -> float | None:
    size = 2 * HALF / 2**BLOCK_Z * math.cos(math.radians(lat))
    area = (blocks[2] - blocks[0] + 1) * (blocks[3] - blocks[1] + 1) * size * size
    return next((float(s) for s in CELL_STEPS if area / (s * s) <= max_cells), None)


class SmartRouter:
    def __init__(self, vchart: Any, dayscore: Any = None, notices: Any = None):
        self.vchart = vchart
        self.dayscore = dayscore
        self.notices = notices
        self._grids: dict[tuple, Grid] = {}
        self._downloading: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def _download(self, keys: list[tuple[int, int]]) -> None:
        for key in keys:
            try:
                await self.vchart.block(*key)
            except Exception:  # noqa: BLE001 - a block that fails is just left out
                _LOGGER.debug("Chart block %s for route planning failed", key, exc_info=True)

    def _read(self, bx: int, by: int, w: int, h: int, need: float, draught: float, cell_m: float):
        """A block's cells (in a worker thread): (classes, fairways) or None with no chart data."""
        chart = self.vchart
        layers: dict[str, Any] = {}
        for part, only in (("", ("depth", "sounding")), ("fw", ("fline", "route")), ("mml", ("rock", "rockarea", "wreck")),
                           ("trk", ("trock",)), ("fx", ("xdepth",))):
            path = chart._path(bx, by, part)
            if not path.exists():
                if part == "":
                    return None
                continue
            try:
                got = read_block(path, LEVEL, only)[1]
            except (OSError, ValueError, EOFError):
                if part == "":
                    return None
                continue
            if part == "fx":
                got = {"xdepth": got["xdepth"]} if "xdepth" in got else {}
            layers.update(got)
        sea = None
        try:
            sea = chart._sea_gaps(bx, by, LEVEL, layers)
        except Exception:  # noqa: BLE001 - only the open sea past the depth areas is lost
            _LOGGER.debug("Sea gaps of block %s,%s failed", bx, by, exc_info=True)
        return render_block(layers, w, h, need, draught, cell_m, sea)

    async def _grid(self, blocks: tuple[int, int, int, int], cell_m: float, need: float, draught: float,
                    start: tuple[float, float], end: tuple[float, float], room: float = 1.0) -> Grid:
        key = (blocks, cell_m, round(need, 1), round(draught, 1), room)
        now = time.monotonic()
        for k in [k for k, g in self._grids.items() if now - g.built > CACHE_SECONDS]:
            del self._grids[k]
        grid = self._grids.get(key)
        if grid is not None and not grid.missing:
            return grid
        grid = Grid(*blocks, cell_m)
        cls = Image.new("L", (grid.w, grid.h), 0)
        fw = Image.new("L", (grid.w, grid.h), 0)
        missing: list[tuple[int, int]] = []
        for by in range(blocks[1], blocks[3] + 1):
            for bx in range(blocks[0], blocks[2] + 1):
                left, top, right, bottom = grid.block_px(bx, by)
                w, h = right - left, bottom - top
                if w <= 0 or h <= 0:
                    continue
                try:
                    got = await asyncio.wait_for(in_thread(self._read, bx, by, w, h, need, draught, cell_m), BLOCK_WAIT)
                except asyncio.TimeoutError:
                    got = None
                if got is None:
                    if near_finland(block_bounds(bx, by)):
                        missing.append((bx, by))
                        continue
                    got = (await in_thread(render_base, bx, by, w, h), None, [])
                    grid.coarse += 1
                cls.paste(got[0], (left, top))
                if got[1] is not None:
                    fw.paste(got[1], (left, top))
                    add_fairway_lines(grid, got[2], left, top)
        grid.missing = len(missing)
        if missing and (self._downloading is None or self._downloading.done()):
            self._downloading = asyncio.create_task(self._download(missing))
        areas = None
        if self.notices is not None:
            (lat1, lon0), (lat0, lon1) = grid.latlon(BORDER, BORDER), grid.latlon(grid.h - BORDER, grid.w - BORDER)
            try:
                areas = await asyncio.wait_for(self.notices.limit_areas(lat0, lon0, lat1, lon1), 20)
                grid.limits_known = True
            except Exception as err:  # noqa: BLE001 - planned without the speed limits then
                _LOGGER.info("Route planning without speed limits: %s", err)
        await in_thread(finish, grid, cls, fw, areas, room)
        grid.built = now
        self._grids[key] = grid
        while len(self._grids) > CACHE_KEEP:
            del self._grids[next(iter(self._grids))]
        return grid

    async def route(self, start: tuple[float, float], end: tuple[float, float], boat_name: str | None = None,
                    draught: float | None = None, speed_kn: float | None = None, depart: float | None = None,
                    style: str = "fast", room: str = "normal") -> dict[str, Any]:
        name, boat = boats.boat(boat_name)
        room = room if room in SHORE_ROOM else "normal"
        sail = boat["kind"] == "sail"
        draught = boat["draught"] if draught is None else draught
        need = draught + boat["keel_margin"]
        speed_kn = speed_kn or boat["cruise_kn"]
        blocks = corridor(start, end)
        count = (blocks[2] - blocks[0] + 1) * (blocks[3] - blocks[1] + 1)
        cell_m = cell_size(blocks, (start[0] + end[0]) / 2, MAX_CELLS_SAIL if sail else MAX_CELLS)
        if count > MAX_BLOCKS or cell_m is None:
            return {"error": "Too far for automatic planning in one go: add a stop on the way."}
        async with self._lock:  # one grid in memory at a time
            grid = await self._grid(blocks, cell_m, need, draught, start, end, SHORE_ROOM[room])
            base = {"boat": name, "room": room, "near_m": round(NEAR_SHORE_M * SHORE_ROOM[room]), "cell_m": cell_m, "blocks": count, "downloading": grid.missing,
                    "coarse": grid.coarse, "limits": grid.limits_known}
            a = nearest_open(grid, *grid.cell(*start))
            b = nearest_open(grid, *grid.cell(*end))
            if a is None or b is None:
                which = "start" if a is None else "end"
                why = (f"Chart data for {grid.missing} of the {count} areas is still downloading. Try again in a minute."
                       if grid.missing else
                       f"There is no water deep enough for this boat ({need:.1f} m) within "
                       f"{SNAP_MAX_M / 1000:.1f} km of the {which}.")
                return {**base, "error": why}
            if sail:
                out = await self._sail(grid, a, b, start, end, depart or time.time(), style)
            else:
                shelter = await self._shelter(grid, boat, depart)
                expo = await in_thread(exposure, grid, shelter.get("wind_from")) if shelter["weight"] > 0 else None
                found = await in_thread(search_power, grid, a, b, speed_kn, expo, shelter["weight"],
                                        tuple(boat.get("fairway") or (FAIRWAY_COST, FAIRWAY_COST)))
                out = None
                if found:
                    idx = await in_thread(self._shape_power, grid, found, speed_kn)
                    out = describe(grid, found["cells"], idx, found, False, start, end)
                    out["shelter"] = shelter
                    out["style"] = "shelter"
                    # The other way to see beside it: the direct one, as any boat would be sent
                    # (no shelter, fairways only a little shorter).
                    plain = await in_thread(search_power, grid, a, b, speed_kn)
                    direct = None
                    if plain:
                        plain["expo"], plain["weight"] = expo, 1e-9 if expo else 0.0  # for its open-water miles
                        pidx = await in_thread(self._shape_power, grid, plain, speed_kn)
                        direct = describe(grid, plain["cells"], pidx, plain, False, start, end)
                        direct["shelter"] = {**shelter, "weight": 0.0} if expo else shelter
                        direct["style"] = "direct"
                    if direct and (abs(direct["metres"] - out["metres"]) > 0.02 * out["metres"]
                                   or abs((direct.get("exposed_nm") or 0) - (out.get("exposed_nm") or 0)) >= 0.3):
                        mine, other = (direct, out) if style == "direct" else (out, direct)
                        mine["alt"] = {k: other.get(k) for k in ("points", "hows", "metres", "seconds", "nm", "style",
                                                                 "exposed_nm", "fairway_nm")}
                        out = mine
                    else:
                        out["alt"] = None
        if not out:
            why = (f"Chart data for {grid.missing} of the {count} areas along the way is still downloading. "
                   "Try again in a minute." if grid.missing else
                   f"No way through water deep enough for this boat ({need:.1f} m) joins the start to the end.")
            return {**base, "error": why}
        warnings = out.setdefault("warnings", [])
        if out["shallow_nm"] >= 0.1:
            warnings.append(f"{out['shallow_nm']} NM runs through water the chart only gives as a depth range that "
                            f"includes less than the {need:.1f} m this boat needs: check those stretches (dashed).")
        for d, label in zip(out["snap_m"], ("start", "end")):
            if d > SNAP_WARN_M:
                warnings.append(f"The {label} is {d} m from water deep enough for this boat: check that straight leg.")
        if grid.missing:
            warnings.append(f"Chart data for {grid.missing} of the {count} areas is still downloading: "
                            "the route avoids them for now.")
        if grid.coarse:
            warnings.append("Part of the way is outside the chart data: only a coarse coastline is known there.")
        return {**base, **out, "need": round(need, 1)}

    async def _shelter(self, grid: Grid, boat: dict[str, Any], depart: float | None) -> dict[str, Any]:
        """How much open water counts longer for this boat, from the wind forecast for the
        middle of the area at the hour of leaving: {"weight", "wind_ms", "wind_from", "at"}."""
        base = float(boat.get("shelter") or 0)
        out: dict[str, Any] = {"weight": base, "wind_ms": None, "wind_from": None, "at": None}
        if base <= 0 or self.dayscore is None:
            return out
        lat, lon = grid.latlon(grid.h / 2, grid.w / 2)
        try:
            air, _sea, _errors = await asyncio.wait_for(
                self.dayscore.conditions([(round(lat, 2), round(lon, 2), 0.0)]), 20)
        except Exception as err:  # noqa: BLE001 - planned for shelter all round then
            _LOGGER.info("Route planning without the wind forecast: %s", err)
            return out
        if air is None or not air.times or "ms" not in air.values:
            return out
        when = depart or time.time()
        j = min(range(len(air.times)), key=lambda i: abs(air.times[i] - when))
        ms = air.values["ms"][0][j]
        if ms is None:
            return out
        low, high = SHELTER_WIND
        out["weight"] = round(base * min(high, max(low, ms / boat["wind"][0])), 2)
        out["wind_ms"] = round(ms, 1)
        out["at"] = air.times[j]
        d = air.values["dir"][0][j] if "dir" in air.values else None
        if d is not None and ms >= 3:
            out["wind_from"] = round(d)
        return out

    @staticmethod
    def _shape_power(grid: Grid, found: dict[str, Any], speed_kn: float) -> list[int]:
        cells = found["cells"]
        idx = straighten(grid, cells, found["g"], max(0.5, speed_kn) * KN, found.get("expo"), found.get("weight") or 0,
                         found.get("fairway") or (FAIRWAY_COST, FAIRWAY_COST))
        tol = 0.4
        while len(idx) > MAX_POINTS and tol < 6:
            sub = [cells[i] for i in idx]
            idx = [idx[i] for i in thin(grid, sub, tol)]
            tol *= 1.6
        return idx

    @staticmethod
    def _shape_sail(grid: Grid, found: dict[str, Any]) -> list[int]:
        cells, how = found["cells"], found["how"]
        # Thinned within each run of the same point of sail, so tacks keep their place.
        idx = [0]
        a = 0
        for i in range(1, len(cells) + 1):
            if i == len(cells) or (i + 1 < len(cells) and how[i + 1] != how[i]):
                b = min(i, len(cells) - 1)
                if b > a:
                    idx += [a + j for j in thin(grid, cells[a:b + 1], 1.2)[1:]]
                a = b
        tol = 2.0
        while len(idx) > MAX_POINTS and tol < 8:
            idx = [idx[i] for i in thin(grid, [cells[i] for i in idx], tol)]
            tol *= 1.6
        return idx

    async def _sail(self, grid: Grid, a: tuple[int, int], b: tuple[int, int], start: tuple[float, float],
                    end: tuple[float, float], depart: float, style: str) -> dict[str, Any] | None:
        pts, shape = wind_points(grid)
        air = sea = None
        errors: dict[str, str] = {}
        if self.dayscore is not None:
            try:
                air, sea, errors = await self.dayscore.conditions(pts)
            except Exception as err:  # noqa: BLE001 - planned without the forecast then
                errors = {"forecast": str(err) or type(err).__name__}
        if air is None or not air.times or not air.has("dir"):
            # No forecast: the engine's way, said so.
            found = await in_thread(search_power, grid, a, b, boats.SAIL["motor_kn"])
            if not found:
                return None
            idx = await in_thread(SmartRouter._shape_power, grid, found, boats.SAIL["motor_kn"])
            out = describe(grid, found["cells"], idx, found, False, start, end)
            out["warnings"] = ["The wind forecast couldn't be read, so this is the way under engine, not a sailing route."]
            out["style"] = "motor"
            return out
        wind = Wind(grid, pts, air, sea, shape)
        results = {}
        for name, comfortable in (("fast", False), ("comfort", True)):
            found = await in_thread(search_sail, grid, a, b, wind, depart, comfortable)
            if not found:
                return None
            idx = await in_thread(self._shape_sail, grid, found)
            results[name] = describe(grid, found["cells"], idx, found, True, start, end)
            results[name]["style"] = name
        style = style if style in results else "fast"
        other = "comfort" if style == "fast" else "fast"
        out = results[style]
        alt = results[other]
        same = alt["points"] == out["points"] or (abs(alt["seconds"] - out["seconds"]) < 0.01 * out["seconds"]
                                                  and abs(alt["metres"] - out["metres"]) < 0.01 * out["metres"])
        out["alt"] = None if same else {k: alt[k] for k in ("points", "hows", "metres", "seconds", "nm", "style")}
        out["same"] = same
        out["depart"] = round(depart)
        out["wind_source"] = air.extra.get("air")
        beyond = depart + out["seconds"] > air.times[-1]
        if beyond:
            out.setdefault("warnings", []).append("The trip runs past the end of the two-day forecast: the last "
                                                  "forecast hour is used from there on.")
        return out


def numbers() -> dict[str, Any]:
    """The planner's own numbers, for the panel's list."""
    return {"cell_steps_m": CELL_STEPS, "max_cells": MAX_CELLS, "max_cells_sail": MAX_CELLS_SAIL,
            "pad_min_m": PAD_MIN_M, "pad_share": PAD_SHARE, "pad_max_m": PAD_MAX_M,
            "marginal_share": MARGINAL_SHARE, "rock_clear_m": ROCK_CLEAR_M, "shore_clear_m": SHORE_CLEAR_M,
            "near_shore_m": NEAR_SHORE_M, "near_shore_cost": NEAR_SHORE_COST, "marginal_cost": MARGINAL_COST,
            "fairway_cost": FAIRWAY_COST, "shore_room": SHORE_ROOM, "check_min_m": CHECK_MIN_M, "shelter_radius_m": SHELTER_RADIUS_M, "shelter_upwind_m": SHELTER_UPWIND_M,
            "shelter_land_share": SHELTER_LAND_SHARE, "shelter_wind": SHELTER_WIND, "fairway_margin_m": DRAUGHT_MARGIN, "snap_max_m": SNAP_MAX_M,
            "wind_grid": WIND_GRID, "headings": 16}
