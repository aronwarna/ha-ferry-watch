"""The drawn chart outside Finnish waters: Swedish, Estonian (and Russian) coasts and seas.

Traficom's data stops at the Finnish sea border, so the rest of the chart is drawn from
open data into the same layers and the same tile format, so it gets the same inks:

- depths from the EMODnet Bathymetry digital terrain model (a grid of about 115 m,
  CC BY 4.0, no key), turned into the same depth bands (0-3, 3-6, 6-10 m and deeper) and
  contours (3, 6, 10, 20, 50, 100, 200 m) as the Finnish chart,
- the coastline from OpenStreetMap (ODbL, through the Overpass API), burnt into the depth
  grid so islands and skerries are where the map has them, and
- sea marks, lights with their characters and sectors, rocks, wrecks, traffic separation
  schemes, leading lines, pilot boarding places and names from OpenStreetMap's seamark
  tagging (OpenSeaMap).

The Finnish chart wins: everything here is cut to outside ``FINNISH_WATERS`` (areas
overlap it by a few hundred metres of water, so no gap shows where the two meet; the
border runs down the middle of the gulfs, where both sides are deeper than 10 m).

RAM: the depth model is fetched once per zoom-9 map tile (about 40 km across, under 1 MB)
and kept on disk as 16-bit decimetres. A block samples it onto a grid of GRID x GRID
nodes (under 1 MB of arrays), OpenStreetMap's answer is parsed as a stream (XML, element
by element) and drawn into that grid as it arrives, so a big archipelago answer is never
held in memory, and what is kept is the usual compact block layers.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import os
import re
import struct
import sys
import time
import zlib
from array import array
from collections import deque
from pathlib import Path
from typing import Any, Callable
from xml.etree.ElementTree import XMLPullParser

import aiohttp

from . import vchart as V
from .charts import FINNISH_WATERS
from .config import USER_AGENT

_LOGGER = logging.getLogger(__name__)

EMODNET_WCS = "https://ows.emodnet-bathymetry.eu/wcs"
EMODNET_COVERAGE = "emodnet__mean"  # the latest DTM's mean depth (elevation, negative under water)
OVERPASS = "https://overpass-api.de/api/interpreter"
ATTRIBUTION = ("Outside Finland: depths EMODnet Bathymetry (CC BY 4.0); coast, sea marks, routeing and "
               "names © OpenStreetMap contributors (ODbL), OpenSeaMap.")

PART = "fx"  # the block part these layers are stored in (see vchart.PARTS)
# Where outside Finnish waters the chart is drawn (lon/lat): the Swedish coast from south of
# Stockholm up the Gulf of Bothnia, the Åland Sea, Estonia and the eastern gulf.
FOREIGN_AREA = (16.5, 57.3, 31.0, 66.2)
DTM_Z = 9  # the depth model is fetched for zoom-9 map tiles
DTM_MAX_AGE = 180 * 86400  # it changes every year or two
GRID = 320  # depth grid nodes across a block and its margin (about 34 m apart at 60°N)
GRID_MARGIN = V.UNITS // 16  # block units of grid past each block edge
OV_GRID = 280  # nodes across an overview tile and its buffer
MASK_M = 700  # web-mercator metres (about 350 m) foreign water reaches into Finnish waters
SHALLOWEST = 0.5  # m: water the map has but the depth model calls land is this deep
LAND, COAST = -2.0, -0.5  # grid values of land and of the coastline itself
NEAR = 1.5  # nodes from the coastline within which the distance to it sets the grid value
MASKED = -50.0  # inside Finnish waters: nothing drawn from here
BANDS = ((0, 3), (3, 6), (6, 10), (10, 1000))  # depth areas as (drval1, drval2)
OVERPASS_PAUSE = 2.0  # s between Overpass requests, as its usage policy asks
OVERPASS_TIMEOUT = 120
OVERPASS_MAX = 48 * 1024 * 1024  # bytes streamed from one answer at most (never held whole)
LANE_HALF_M = 1500  # ground metres each side of a lane's centre line drawn as the lane
FAILED_RETRY = 3600

# --- the Finnish border ---------------------------------------------------------------

FINNISH_M = V.FINNISH_WATERS_M
# Everything that isn't Finland, as one simple polygon that shares FINNISH_WATERS's sea
# border (its first 23 points, Tornio to the Russian border) and its land border round
# Lapland, so the land painted on either side meets without a gap or an overlap.
_SEA_BORDER = list(FINNISH_WATERS[:23])
FOREIGN = _SEA_BORDER + [(31.0, 59.0), (31.0, 53.5), (9.0, 53.5), (9.0, 68.0), (20.6, 69.0),
                         (23.6, 67.9), (23.9, 66.8)]
FOREIGN_M = [V.lonlat_to_mercator(lon, lat) for lon, lat in FOREIGN]


def _poly_spans(poly: list[tuple[float, float]], y: float) -> list[float]:
    """x of each crossing of a polygon's edges with a horizontal line, sorted."""
    xs = []
    n = len(poly)
    for i in range(n):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
        if (ay > y) != (by > y):
            xs.append(ax + (bx - ax) * (y - ay) / (by - ay))
    xs.sort()
    return xs


def _inside_spans(xs: list[float], x: float) -> bool:
    lo, hi = 0, len(xs)
    while lo < hi:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid + 1
        else:
            hi = mid
    return lo % 2 == 1


def in_finland_m(x: float, y: float) -> bool:
    return _inside_spans(_poly_spans(FINNISH_M, y), x)


def _area(ring: list) -> float:
    return abs(sum(ax * by - bx * ay for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1]))) / 2


def foreign_ring(bounds: tuple[float, float, float, float], to_local: Callable) -> list:
    """The part of FOREIGN in a web-mercator box, in local coordinates (empty if none)."""
    ring = V.clip_ring(FOREIGN_M, *bounds)
    return [to_local(x, y) for x, y in ring] if len(ring) >= 3 and _area(ring) > 1.0 else []


def wanted(bounds: tuple[float, float, float, float]) -> bool:
    """Whether a web-mercator box needs foreign data: it is in FOREIGN_AREA and not all Finnish."""
    lon0, lat0 = V.mercator_to_lonlat(bounds[0], bounds[1])
    lon1, lat1 = V.mercator_to_lonlat(bounds[2], bounds[3])
    a = FOREIGN_AREA
    if lon1 < a[0] or lon0 > a[2] or lat1 < a[1] or lat0 > a[3]:
        return False
    return bool(foreign_ring(bounds, lambda x, y: (x, y)))


def convex_pieces(bounds: tuple[float, float, float, float], to_local: Callable) -> list[list]:
    """FOREIGN in a box as convex pieces (local coordinates): the box is cut into strips at
    FOREIGN's corners inside it, and in each strip the border is one straight line."""
    x0, y0, x1, y1 = bounds
    cuts = sorted({x for x, y in FOREIGN_M if x0 < x < x1 and y0 < y < y1})
    edges = [x0, *cuts, x1]
    pieces = []
    for a, b in zip(edges, edges[1:]):
        ring = V.clip_ring(FOREIGN_M, a, y0, b, y1)
        if len(ring) >= 3 and _area(ring) > 1.0:
            pieces.append([to_local(x, y) for x, y in ring])
    return pieces


def _orient_ccw(poly: list) -> list:
    s = sum(ax * by - bx * ay for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]))
    return poly if s > 0 else poly[::-1]


def clip_convex(ring: list, clip: list) -> list:
    """Sutherland-Hodgman against a convex polygon."""
    clip = _orient_ccw(clip)
    out = ring
    n = len(clip)
    for i in range(n):
        if not out:
            break
        (ax, ay), (bx, by) = clip[i], clip[(i + 1) % n]
        ex, ey = bx - ax, by - ay

        def side(p):
            return ex * (p[1] - ay) - ey * (p[0] - ax)

        src, out = out, []
        prev = src[-1]
        sp = side(prev)
        for cur in src:
            sc = side(cur)
            if (sc >= 0) != (sp >= 0):
                t = sp / (sp - sc)
                out.append((prev[0] + (cur[0] - prev[0]) * t, prev[1] + (cur[1] - prev[1]) * t))
            if sc >= 0:
                out.append(cur)
            prev, sp = cur, sc
    return out


def clip_line_convex(pts: list, clip: list) -> list[list]:
    """Cyrus-Beck per segment: the pieces of a line inside a convex polygon."""
    clip = _orient_ccw(clip)
    n = len(clip)
    parts: list[list] = []
    cur: list = []
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        t0, t1 = 0.0, 1.0
        dx, dy = bx - ax, by - ay
        ok = True
        for i in range(n):
            (cx, cy), (ex_, ey_) = clip[i], clip[(i + 1) % n]
            nx, ny = -(ey_ - cy), ex_ - cx  # inward normal of a ccw polygon
            num = nx * (ax - cx) + ny * (ay - cy)
            den = nx * dx + ny * dy
            if den == 0:
                if num < 0:
                    ok = False
                    break
                continue
            t = -num / den
            if den > 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                ok = False
                break
        if not ok:
            if len(cur) > 1:
                parts.append(cur)
            cur = []
            continue
        start, end = (ax + t0 * dx, ay + t0 * dy), (ax + t1 * dx, ay + t1 * dy)
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


# --- the depth model --------------------------------------------------------------------

class TiffError(ValueError):
    pass


def _lzw_decode(data: bytes) -> bytes:
    """TIFF's LZW (MSB-first codes, early change)."""
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    bits, width, buf, prev = 0, 9, 0, None
    pos = 0
    n = len(data)
    while True:
        while bits < width and pos < n:
            buf = (buf << 8) | data[pos]
            pos += 1
            bits += 8
        if bits < width:
            break
        bits -= width
        code = (buf >> bits) & ((1 << width) - 1)
        if code == 256:
            table = table[:258]
            width, prev = 9, None
            continue
        if code == 257:
            break
        if prev is None:
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            table.append(prev + entry[:1])
        else:
            entry = prev + prev[:1]
            table.append(entry)
        out += entry
        prev = entry
        if len(table) + 1 >= (1 << width) and width < 12:
            width += 1
    return bytes(out)


class Grid:
    """A depth model grid: elevation in decimetres (negative under water) as int16, north up.
    ``lon0, lat0`` is the north-west corner of the first cell; values are at cell centres."""

    NODATA = -32768

    def __init__(self, w: int, h: int, lon0: float, lat0: float, dlon: float, dlat: float, values: array):
        self.w, self.h, self.lon0, self.lat0, self.dlon, self.dlat, self.values = w, h, lon0, lat0, dlon, dlat, values

    def to_bytes(self) -> bytes:
        head = json.dumps({"w": self.w, "h": self.h, "lon0": self.lon0, "lat0": self.lat0,
                           "dlon": self.dlon, "dlat": self.dlat}).encode()
        vals = array("h", self.values)
        if sys.byteorder != "little":
            vals.byteswap()
        return head + b"\n" + vals.tobytes()

    @classmethod
    def from_bytes(cls, data: bytes) -> "Grid":
        head, _, raw = data.partition(b"\n")
        h = json.loads(head)
        vals = array("h")
        vals.frombytes(raw)
        if sys.byteorder != "little":
            vals.byteswap()
        if len(vals) != h["w"] * h["h"]:
            raise ValueError("depth grid file is cut short")
        return cls(h["w"], h["h"], h["lon0"], h["lat0"], h["dlon"], h["dlat"], vals)

    def depth(self, lon: float, lat: float) -> float | None:
        """Depth in metres (positive under water, negative on land), bilinear, or None
        outside the grid or where it has no data."""
        fx = (lon - self.lon0) / self.dlon - 0.5
        fy = (self.lat0 - lat) / self.dlat - 0.5
        if fx < -0.5 or fy < -0.5 or fx > self.w - 0.5 or fy > self.h - 0.5:
            return None
        ix = min(max(int(math.floor(fx)), 0), self.w - 2) if self.w > 1 else 0
        iy = min(max(int(math.floor(fy)), 0), self.h - 2) if self.h > 1 else 0
        tx = min(max(fx - ix, 0.0), 1.0)
        ty = min(max(fy - iy, 0.0), 1.0)
        v = self.values
        w = self.w
        corners = []
        for dy, wy in ((0, 1 - ty), (1, ty)):
            for dx, wx in ((0, 1 - tx), (1, tx)):
                jx, jy = min(ix + dx, self.w - 1), min(iy + dy, self.h - 1)
                corners.append((v[jy * w + jx], wx * wy))
        good = [(val, wt) for val, wt in corners if val != self.NODATA]
        if not good:
            return None
        if len(good) < 4:
            val = max(good, key=lambda g: g[1])[0]  # the nearest with data
            return -val / 10
        return -sum(val * wt for val, wt in good) / 10


def parse_geotiff(data: bytes) -> Grid:
    """A single-band GeoTIFF in degrees (as EMODnet's WCS sends) as a Grid. Strips or tiles,
    uncompressed, Deflate or LZW, integer or float samples."""
    if data[:4] == b"II*\x00":
        e = "<"
    elif data[:4] == b"MM\x00*":
        e = ">"
    else:
        raise TiffError("not a TIFF: " + bytes(data[:200]).decode("utf-8", "replace").strip()[:200])
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8}
    codes = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d", 16: "Q"}
    (ifd,) = struct.unpack_from(e + "I", data, 4)
    (count,) = struct.unpack_from(e + "H", data, ifd)
    tags: dict[int, Any] = {}
    for k in range(count):
        tag, typ, n, = struct.unpack_from(e + "HHI", data, ifd + 2 + 12 * k)
        size = sizes.get(typ, 1) * n
        off = ifd + 2 + 12 * k + 8 if size <= 4 else struct.unpack_from(e + "I", data, ifd + 2 + 12 * k + 8)[0]
        if typ == 2:
            tags[tag] = bytes(data[off:off + n]).rstrip(b"\x00").decode("ascii", "replace")
        elif typ in codes:
            tags[tag] = list(struct.unpack_from(e + codes[typ] * n, data, off))
        elif typ in (5, 10):
            raw = struct.unpack_from(e + ("II" if typ == 5 else "ii") * n, data, off)
            tags[tag] = [raw[2 * i] / raw[2 * i + 1] if raw[2 * i + 1] else 0 for i in range(n)]
    w, h = tags[256][0], tags[257][0]
    bits = tags.get(258, [8])[0]
    fmt = tags.get(339, [1])[0]
    comp = tags.get(259, [1])[0]
    pred = tags.get(317, [1])[0]
    if tags.get(277, [1])[0] != 1:
        raise TiffError("more than one band")
    code = {(3, 32): "f", (3, 64): "d", (2, 16): "h", (2, 32): "i", (1, 16): "H", (1, 8): "B", (2, 8): "b",
            (1, 32): "I"}.get((fmt, bits))
    if code is None:
        raise TiffError(f"sample format {fmt}/{bits} bits")
    if comp not in (1, 5, 8, 32946):
        raise TiffError(f"compression {comp}")
    if pred not in (1, 2, 3):
        raise TiffError(f"predictor {pred}")
    nodata = None
    if 42113 in tags:
        try:
            nodata = float(str(tags[42113]).strip())
        except ValueError:
            nodata = None
    scale = tags.get(33550)
    tie = tags.get(33922)
    if not scale or not tie:
        raise TiffError("no georeference")
    dlon, dlat = scale[0], scale[1]
    lon0 = tie[3] - tie[0] * dlon
    lat0 = tie[4] + tie[1] * dlat
    if 322 in tags:
        tw, th = tags[322][0], tags[323][0]
        offsets, counts = tags[324], tags[325]
    else:
        tw, th = w, tags.get(278, [h])[0]
        offsets, counts = tags[273], tags[279]
    out = array("h", [Grid.NODATA]) * (w * h)
    bpp = bits // 8
    across = (w + tw - 1) // tw
    for idx, (off, n) in enumerate(zip(offsets, counts)):
        raw = bytes(data[off:off + n])
        if comp in (8, 32946):
            raw = zlib.decompress(raw)
        elif comp == 5:
            raw = _lzw_decode(raw)
        rows = len(raw) // (tw * bpp)
        if pred == 2 and code not in "fd":
            # Horizontal differencing: undone on the unsigned samples, a row at a time.
            u = array({1: "B", 2: "H", 4: "I"}[bpp])
            u.frombytes(raw[:rows * tw * bpp])
            if (e == "<") != (sys.byteorder == "little"):
                u.byteswap()
            mask = (1 << bits) - 1
            for r in range(rows):
                for c in range(r * tw + 1, (r + 1) * tw):
                    u[c] = (u[c] + u[c - 1]) & mask
            if (e == "<") != (sys.byteorder == "little"):
                u.byteswap()
            raw = u.tobytes()
            del u
        swap = (e == "<") != (sys.byteorder == "little")
        if pred == 3:
            # The floating-point predictor: bytes differenced along each row, stored as planes
            # of the samples' bytes, most significant first.
            rowb = tw * bpp
            buf = bytearray(raw[:rows * rowb])
            for r in range(rows):
                o = r * rowb
                for c in range(o + 1, o + rowb):
                    buf[c] = (buf[c] + buf[c - 1]) & 0xFF
                planes = bytes(buf[o:o + rowb])
                for c in range(tw):
                    for b in range(bpp):
                        buf[o + c * bpp + b] = planes[b * tw + c]
            raw = bytes(buf)
            del buf
            swap = sys.byteorder == "little"  # now big-endian whatever the file's order
        # Samples as a typed array (no Python object each), a strip at a time.
        vals = array(code)
        vals.frombytes(raw[:rows * tw * bpp])
        del raw
        if swap:
            vals.byteswap()
        bx, by = (idx % across) * tw, (idx // across) * th
        for r in range(rows):
            y = by + r
            if y >= h:
                break
            base = y * w
            for c in range(min(tw, w - bx)):
                v = vals[r * tw + c]
                if v != v or (nodata is not None and abs(v - nodata) < 1e-6) or abs(v) > 3000:
                    continue
                out[base + bx + c] = max(-32000, min(32000, round(v * 10)))
        del vals
    return Grid(w, h, lon0, lat0, dlon, dlat, out)


def dtm_tile_bounds(x: int, y: int) -> tuple[float, float, float, float]:
    """lon0, lat0, lon1, lat1 of a zoom-DTM_Z map tile."""
    minx, miny, maxx, maxy = V._tile_mercator(DTM_Z, x, y)
    lon0, lat0 = V.mercator_to_lonlat(minx, miny)
    lon1, lat1 = V.mercator_to_lonlat(maxx, maxy)
    return lon0, lat0, lon1, lat1


def dtm_tiles_for(bounds: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """Zoom-DTM_Z tiles a web-mercator box touches that need foreign data."""
    size = 2 * V.HALF / 2**DTM_Z
    x0, x1 = int((bounds[0] + V.HALF) // size), int((bounds[2] + V.HALF) // size)
    y0, y1 = int((V.HALF - bounds[3]) // size), int((V.HALF - bounds[1]) // size)
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)
            if wanted(V._tile_mercator(DTM_Z, x, y))]


class Sampler:
    """Depths at points from the grids of several zoom-9 tiles."""

    def __init__(self, grids: dict[tuple[int, int], Grid]):
        self.grids = grids
        self.size = 2 * V.HALF / 2**DTM_Z

    def depth_m(self, mx: float, my: float, lon: float, lat: float) -> float | None:
        key = (int((mx + V.HALF) // self.size), int((V.HALF - my) // self.size))
        g = self.grids.get(key)
        return g.depth(lon, lat) if g is not None else None


# --- marching squares -------------------------------------------------------------------

def _case_table() -> list[list[list[tuple[str, str]]]]:
    """Oriented segments for each of the 16 cases (TL=8, TR=4, BR=2, BL=1 above the level), for
    a saddle's centre below [0] and above [1]: the region above is always on the same side."""
    mid = {"T": (0.5, 0), "R": (1, 0.5), "B": (0.5, 1), "L": (0, 0.5)}
    corner = ((0, 0), (1, 0), (1, 1), (0, 1))
    ends = {"T": (0, 1), "R": (1, 2), "B": (2, 3), "L": (3, 0)}
    near = {0: ("L", "T"), 1: ("T", "R"), 2: ("R", "B"), 3: ("B", "L")}

    def orient(e1, e2, ref, ref_inside):
        (ax, ay), (bx, by) = mid[e1], mid[e2]
        cross = (bx - ax) * (ref[1] - ay) - (by - ay) * (ref[0] - ax)
        if (cross < 0) != ref_inside:
            return e2, e1
        return e1, e2

    table = []
    for case in range(16):
        inside = [(case >> (3 - k)) & 1 for k in range(4)]
        crossed = [e for e, (a, b) in ends.items() if inside[a] != inside[b]]
        variants = []
        for centre in (0, 1):
            segs = []
            if len(crossed) == 2:
                ins = [corner[k] for k in range(4) if inside[k]]
                ref = (sum(p[0] for p in ins) / len(ins), sum(p[1] for p in ins) / len(ins))
                segs.append(orient(crossed[0], crossed[1], ref, True))
            elif len(crossed) == 4:
                for k in range(4):
                    # Above the level in the middle: the corners below are cut off; else those above.
                    if inside[k] != centre:
                        e1, e2 = near[k]
                        segs.append(orient(e1, e2, corner[k], bool(inside[k])))
            variants.append(segs)
        table.append(variants)
    return table


CASES = _case_table()


def march(rows: list[list[float]], level: float, closed: bool, skip: Callable[[int, int], bool] | None = None
          ) -> list[list[tuple[float, float]]]:
    """Iso-lines of a grid of values (rows of nodes, which must have no NaN) at a level.

    ``closed``: rings round the region above the level (the grid's edge counts as below, so
    every ring closes). Otherwise contour lines; ``skip(i, j)`` leaves out cell (i, j)
    (whose top-left node is column i, row j). Coordinates are in nodes."""
    h = len(rows)
    w = len(rows[0]) if h else 0
    if closed:
        edge = array("d", [MASKED - 1])
        pad = edge * (w + 2)
        rows = [pad] + [edge + array("d", r) + edge for r in rows] + [pad]
        off, W, H = -1, w + 2, h + 2
    else:
        off, W, H = 0, w, h
    nxt: dict[int, int] = {}
    pts: dict[int, tuple[float, float]] = {}

    def point(eid: int) -> tuple[float, float]:
        p = pts.get(eid)
        if p is None:
            k, vert = eid >> 1, eid & 1
            i, j = k % W, k // W
            a = rows[j][i]
            b = rows[j + 1][i] if vert else rows[j][i + 1]
            if b <= MASKED or a <= MASKED:
                # Up to a masked node (or the grid's edge) itself, whatever the level, so the
                # areas of every level end along the same line there.
                f = 1.0 if b <= MASKED else 0.0
            else:
                f = (level - a) / (b - a) if b != a else 0.5
            p = (i + off, j + f + off) if vert else (i + f + off, j + off)
            pts[eid] = p
        return p

    for j in range(H - 1):
        r0, r1 = rows[j], rows[j + 1]
        a0 = [v > level for v in r0]
        a1 = [v > level for v in r1]
        if a0 == a1 and (not any(a0) or all(a0)):
            continue
        for i in range(W - 1):
            case = a0[i] << 3 | a0[i + 1] << 2 | a1[i + 1] << 1 | a1[i]
            if case == 0 or case == 15:
                continue
            if skip is not None and skip(i, j):
                continue
            centre = 0
            if case in (5, 10):
                centre = 1 if (r0[i] + r0[i + 1] + r1[i] + r1[i + 1]) / 4 > level else 0
            for e1, e2 in CASES[case][centre]:
                ids = []
                for e in (e1, e2):
                    if e == "T":
                        ids.append(2 * (j * W + i))
                    elif e == "B":
                        ids.append(2 * ((j + 1) * W + i))
                    elif e == "L":
                        ids.append(2 * (j * W + i) + 1)
                    else:
                        ids.append(2 * (j * W + i + 1) + 1)
                nxt[ids[0]] = ids[1]
    out = []
    if not closed:
        ends = set(nxt.values())
        for start in [s for s in nxt if s not in ends]:
            line = [point(start)]
            cur = start
            while cur in nxt:
                cur = nxt.pop(cur)
                line.append(point(cur))
            if len(line) >= 2:
                out.append(line)
    while nxt:
        start, cur = next(iter(nxt.items()))
        del nxt[start]
        ring = [point(start)]
        while cur != start and cur in nxt:
            ring.append(point(cur))
            cur = nxt.pop(cur)
        if len(ring) >= 3:
            out.append(ring + [ring[0]] if not closed else ring)
    return out


def _simplify_ring(ring: list, tol: float) -> list:
    return V.simplify(ring + ring[:1], tol)[:-1] if len(ring) >= 3 else ring


def band_rings(outer: list[list], holes: list[list], box: tuple[float, float, float, float]) -> list[list]:
    """A depth band, the region above one level less the region above the next (``holes``,
    which lie inside ``outer``), as rings for an even-odd fill without the stretches of the
    clip box both run along: the panel outlines areas in their own colour, so a hole's edge
    along the box would draw a line of the shallower band over the deeper one there."""
    if not holes:
        return outer
    x0, y0, x1, y1 = box
    segs: list[tuple] = []
    twins: dict[tuple, int] = {}  # a hole edge that runs back along an outer edge cancels it
    runs: dict[int, list[tuple[float, float]]] = {0: [], 1: [], 2: [], 3: []}

    def add(p, q) -> None:
        if p == q:
            return
        if p[0] == q[0] and p[0] in (x0, x1):
            runs[0 if p[0] == x0 else 1].append((p[1], q[1]))
        elif p[1] == q[1] and p[1] in (y0, y1):
            runs[2 if p[1] == y0 else 3].append((p[0], q[0]))
        elif twins.get((q, p)):
            twins[(q, p)] -= 1
        else:
            twins[(p, q)] = twins.get((p, q), 0) + 1

    for ring in outer:
        for p, q in zip(ring, ring[1:] + ring[:1]):
            add(p, q)
    for ring in holes:
        for p, q in zip(ring, ring[1:] + ring[:1]):
            add(q, p)
    segs = [seg for seg, count in twins.items() for _ in range(count)]
    for side, items in runs.items():
        if not items:
            continue
        cuts = sorted({t for run in items for t in run})
        net = [0] * len(cuts)
        index = {t: k for k, t in enumerate(cuts)}
        for a, b in items:
            sign = 1 if b > a else -1
            for k in range(index[min(a, b)], index[max(a, b)]):
                net[k] += sign
        k = 0
        while k < len(cuts) - 1:
            if net[k] == 0:
                k += 1
                continue
            m = k
            while m + 1 < len(cuts) - 1 and net[m + 1] == net[k]:
                m += 1
            a, b = cuts[k], cuts[m + 1]
            if net[k] < 0:
                a, b = b, a
            fixed = (x0, x1, y0, y1)[side]
            segs.append(((fixed, a), (fixed, b)) if side < 2 else ((a, fixed), (b, fixed)))
            k = m + 1
    starts: dict[tuple, list[tuple]] = {}
    for p, q in segs:
        starts.setdefault(p, []).append(q)
    rings = []
    while starts:
        start = next(iter(starts))
        ring = [start]
        cur = start
        while True:
            ends = starts.get(cur)
            if not ends:
                ring = []
                break
            nxt = ends.pop()
            if not ends:
                del starts[cur]
            if nxt == start:
                break
            ring.append(nxt)
            cur = nxt
        if len(ring) >= 3:
            rings.append(ring)
    return rings


# --- the coastline in the grid ----------------------------------------------------------

class CoastGrid:
    """Nodes of a block's grid marked coast, with water and land seeds beside the coastline
    (OpenStreetMap's coastline has the land on its left)."""

    def __init__(self, n: int):
        self.n = n
        self.cls = bytearray(n * n)  # 1 coast
        self.seed = bytearray(n * n)  # 1 water, 2 land
        # Signed distance (in nodes) to the nearest coastline, water positive, for the nodes
        # near it: the shore is then drawn where the line is, not along the grid.
        self.dist = array("f", [NEAR]) * (n * n)
        self.lines = 0

    def add(self, pts: list[tuple[float, float]]) -> None:
        """A coastline way in node coordinates (x right, y down)."""
        n = self.n
        cls, seed = self.cls, self.seed
        self.lines += 1
        if len(pts) >= 4 and pts[0] == pts[-1]:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            if max(xs) - min(xs) < 2 and max(ys) - min(ys) < 2:
                # A skerry smaller than the grid: land at its nearest node, so it still shows.
                cx, cy = round(sum(xs) / len(xs)), round(sum(ys) / len(ys))
                if 0 <= cx < n and 0 <= cy < n:
                    self.dist[cy * n + cx] = -1.0
                    cls[cy * n + cx] = 1
                return
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            if max(ax, bx) < -2 or min(ax, bx) > n + 1 or max(ay, by) < -2 or min(ay, by) > n + 1:
                continue
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy)
            if length == 0:
                continue
            # In y-down coordinates the left of travel (the land) is (dy, -dx).
            lx, ly = dy / length * 1.1, -dx / length * 1.1
            dist = self.dist
            l2 = length * length
            for jy in range(max(0, int(min(ay, by)) - 1), min(n, int(max(ay, by)) + 3)):
                for jx in range(max(0, int(min(ax, bx)) - 1), min(n, int(max(ax, bx)) + 3)):
                    t = ((jx - ax) * dx + (jy - ay) * dy) / l2
                    t = 0.0 if t < 0 else 1.0 if t > 1 else t
                    d = math.hypot(jx - ax - t * dx, jy - ay - t * dy)
                    k = jy * n + jx
                    if d < abs(dist[k]):
                        side = (jy - ay) * dx - (jx - ax) * dy  # > 0 on the water side
                        dist[k] = d if side >= 0 else -d
            steps = int(length * 2) + 1
            px = py = None
            for s in range(steps + 1):
                x, y = ax + dx * s / steps, ay + dy * s / steps
                ix, iy = round(x), round(y)
                if px is not None and ix != px and iy != py and 0 <= px < n and 0 <= iy < n:
                    cls[iy * n + px] = 1  # keep the line 4-connected: no water leaks diagonally
                if 0 <= ix < n and 0 <= iy < n:
                    cls[iy * n + ix] = 1
                px, py = ix, iy
                for sx, sy, mark in ((x - lx, y - ly, 1), (x + lx, y + ly, 2)):
                    jx, jy = round(sx), round(sy)
                    if 0 <= jx < n and 0 <= jy < n:
                        seed[jy * n + jx] |= mark


def classify(n: int, depth: array, coast: CoastGrid | None) -> array:
    """Grid values for marching: depth in water (at least SHALLOWEST), LAND, COAST. Without a
    coastline the depth model alone says what is water. ``depth`` has NaN where it's unknown."""
    vals = array("d", bytes(8 * n * n))
    if coast is None or not coast.lines:
        for k, d in enumerate(depth):
            vals[k] = max(d, SHALLOWEST) if d > 0 else LAND  # NaN > 0 is False
        return vals
    cls, seed = coast.cls, coast.seed
    label = array("i", [-1]) * (n * n)
    comps: list[list[float]] = []  # water seeds, land seeds, depth sum, nodes
    for start in range(n * n):
        if cls[start] or label[start] >= 0:
            continue
        c = len(comps)
        stats = [0, 0, 0.0, 0]
        label[start] = c
        queue = deque([start])
        while queue:
            k = queue.popleft()
            s = seed[k]
            if s & 1:
                stats[0] += 1
            if s & 2:
                stats[1] += 1
            d = depth[k]
            stats[2] += d if d == d else -1.0
            stats[3] += 1
            x = k % n
            for m in ((k - 1) if x > 0 else -1, (k + 1) if x < n - 1 else -1, k - n, k + n):
                if 0 <= m < n * n and not cls[m] and label[m] < 0:
                    label[m] = c
                    queue.append(m)
        comps.append(stats)
    water = [s[0] > s[1] if s[0] != s[1] else s[2] / max(1, s[3]) > 0.5 for s in comps]
    dist = coast.dist
    for k in range(n * n):
        dk = dist[k]
        if dk != NEAR and abs(dk) < NEAR:
            # Near the line its own side and distance decide, so the 0 level lies on it.
            vals[k] = 2 * dk if dk > 0 else max(LAND, 2 * dk)
        elif cls[k]:
            vals[k] = COAST
        elif water[label[k]]:
            d = depth[k]
            vals[k] = max(d, SHALLOWEST) if d == d else 2.0
        else:
            vals[k] = LAND
    return vals


# --- OpenStreetMap ---------------------------------------------------------------------

def overpass_coast_query(s: float, w: float, n: float, e: float) -> str:
    return f'[out:xml][timeout:{OVERPASS_TIMEOUT - 20}];way["natural"="coastline"]({s:.5f},{w:.5f},{n:.5f},{e:.5f});out skel geom qt;'


ROUTEING = ("separation_zone|separation_line|separation_boundary|separation_lane|separation_roundabout|"
            "precautionary_area|inshore_traffic_zone|deep_water_route|deep_water_route_part|two-way_route|"
            "two-way_route_part|recommended_track|navigation_line|fairway")


def overpass_marks_query(points: tuple, lines: tuple) -> str:
    """Sea marks, rocks and wrecks, and names in ``points`` (s, w, n, e); routeing in ``lines``."""
    pb = "({:.5f},{:.5f},{:.5f},{:.5f})".format(*points)
    lb = "({:.5f},{:.5f},{:.5f},{:.5f})".format(*lines)
    return (f'[out:xml][timeout:{OVERPASS_TIMEOUT - 20}];'
            f'node["seamark:type"]{pb};out qt;'
            f'(way["seamark:type"~"^({ROUTEING})$"]{lb};relation["seamark:type"~"^({ROUTEING})$"]{lb};);'
            f'out geom qt;'
            f'(nwr["place"~"^(island|islet|city|town|village|sea)$"]["name"]{pb};'
            f'nwr["natural"~"^(bay|strait|cape)$"]["name"]{pb};)->.names;'
            f'node.names;out qt;(way.names;relation.names;);out tags center qt;')


class OsmStream:
    """OpenStreetMap XML parsed as it arrives: each finished element is handed on as a dict
    {"type", "tags", "lat"/"lon" | "geometry": [(lon, lat), ...] | "members": [{"role",
    "geometry"}] | "center": (lon, lat)} and dropped."""

    def __init__(self, sink: Callable[[dict], None]):
        self.parser = XMLPullParser(events=("start", "end"))
        self.sink = sink
        self.cur: dict | None = None
        self.member: dict | None = None
        self.depth = 0
        self.remark: str | None = None

    def feed(self, chunk: bytes) -> None:
        self.parser.feed(chunk)
        self._drain()

    def close(self) -> None:
        self.parser.close()
        self._drain()

    def _drain(self) -> None:
        for event, el in self.parser.read_events():
            tag = el.tag
            if event == "start":
                a = el.attrib
                if tag in ("node", "way", "relation") and self.cur is None:
                    self.cur = {"type": tag, "tags": {}}
                    if tag == "node" and "lat" in a:
                        self.cur["lat"], self.cur["lon"] = float(a["lat"]), float(a["lon"])
                    elif tag == "way":
                        self.cur["geometry"] = []
                    elif tag == "relation":
                        self.cur["members"] = []
                elif self.cur is None:
                    continue
                elif tag == "tag":
                    self.cur["tags"][a.get("k", "")] = a.get("v", "")
                elif tag == "nd" and "lat" in a:
                    target = self.member["geometry"] if self.member is not None else self.cur.get("geometry")
                    if target is not None:
                        target.append((float(a["lon"]), float(a["lat"])))
                elif tag == "member":
                    self.member = {"role": a.get("role", ""), "geometry": []}
                    if "lat" in a:
                        self.member["geometry"].append((float(a["lon"]), float(a["lat"])))
                elif tag == "center":
                    self.cur["center"] = (float(a["lon"]), float(a["lat"]))
            else:
                if tag == "member" and self.cur is not None and self.member is not None:
                    self.cur["members"].append(self.member)
                    self.member = None
                elif tag in ("node", "way", "relation") and self.cur is not None and self.cur["type"] == tag:
                    item, self.cur = self.cur, None
                    self.sink(item)
                elif tag == "remark":
                    self.remark = (el.text or "").strip()
                el.clear()


def join_ways(ways: list[list]) -> list[list]:
    """Ways joined end to end into rings (multipolygon members come in pieces)."""
    rings, open_ = [], [list(w) for w in ways if len(w) >= 2]
    while open_:
        cur = open_.pop()
        changed = True
        while cur[0] != cur[-1] and changed:
            changed = False
            for k, w in enumerate(open_):
                if w[0] == cur[-1]:
                    cur += w[1:]
                elif w[-1] == cur[-1]:
                    cur += w[::-1][1:]
                elif w[-1] == cur[0]:
                    cur = w + cur[1:]
                elif w[0] == cur[0]:
                    cur = w[::-1] + cur[1:]
                else:
                    continue
                open_.pop(k)
                changed = True
                break
        if len(cur) >= 4:
            rings.append(cur)
    return rings


# OSM seamark types as the Finnish aid types (vchart.AID_TYPES) and categories.
LATERAL = {"port": 1, "starboard": 2, "preferred_channel_port": 2, "preferred_channel_starboard": 1}
CARDINAL = {"north": 3, "south": 4, "west": 5, "east": 6}
COLOURS = {"white": "v", "red": "p", "green": "vi", "yellow": "k", "orange": "k", "amber": "k"}
LETTERS = {"v": "W", "p": "R", "vi": "G", "k": "Y"}
ROCK_CODES = {"submerged": 38511, "covers": 38512, "awash": 38512, "dry": 38513, "always_dry": 38513,
              "floating": 38511}
WRECK_CODES = {"dangerous": 16721, "non-dangerous": 16722, "distributed_remains": 16722,
               "mast_showing": 16712, "hull_showing": 16712}
TSS_AREAS = {"separation_zone": "zone", "separation_roundabout": "zone", "precautionary_area": "prec",
             "inshore_traffic_zone": "itz", "deep_water_route": "deep", "deep_water_route_part": "deep",
             "two-way_route": "twoway", "two-way_route_part": "twoway", "separation_lane": "lane"}
TSS_LINES = {"separation_line": "sep", "separation_boundary": "bnd"}
NAME_CODES = {"city": 48111, "town": 48111, "village": 48120, "island": 35060, "islet": 35060,
              "cape": 35070, "bay": 36490, "strait": 36490, "sea": 36490}


def _num(v: Any) -> float | None:
    try:
        return float(str(v).strip().split(";")[0])
    except (TypeError, ValueError):
        return None


def aid_props(tags: dict[str, str]) -> tuple | None:
    """A sea mark as the Finnish aid layer's (type, category, lit, name), or None."""
    st = tags.get("seamark:type", "")
    lit = "K" if any(k.startswith("seamark:light") for k in tags) else "E"
    name = tags.get("seamark:name") or tags.get("name")
    sub = st.split("_", 1)[1] if "_" in st else ""
    cat = None
    if sub == "lateral":
        cat = LATERAL.get(tags.get(f"seamark:{st}:category", ""))
    elif sub == "cardinal":
        cat = CARDINAL.get(tags.get(f"seamark:{st}:category", ""))
    elif sub == "isolated_danger":
        cat = 7
    elif sub == "safe_water":
        cat = 8
    elif sub == "special_purpose":
        cat = 9
    shape = tags.get(f"seamark:{st}:shape", "")
    if st.startswith("buoy_"):
        kind = "viitta" if shape in ("spar", "stake", "perch") else "poiju"
    elif st.startswith("beacon_"):
        kind = "kummeli" if shape == "cairn" else "reunamerkki"
    elif st in ("light_major", "light_vessel", "light_float"):
        kind = "merimajakka"
    elif st == "light_minor":
        sectors = any(re.match(r"seamark:light:\d+:sector_start", k) for k in tags)
        kind = "sektoriloisto" if sectors else "apuloisto"
    elif st == "landmark" and lit == "K":
        rng = _num(tags.get("seamark:light:range") or tags.get("seamark:light:1:range"))
        kind = "merimajakka" if rng is not None and rng >= 10 else "apuloisto"
    else:
        return None
    return (kind, cat if cat is not None else 99, lit, name)


def _light_sectors(tags: dict[str, str]) -> list[dict[str, str]]:
    """A light's sectors (or the light itself) as dicts of colour, character, ... ."""
    out: dict[str, dict[str, str]] = {}
    for k, v in tags.items():
        m = re.match(r"seamark:light:(?:(\d+):)?(\w+)$", k)
        if m:
            out.setdefault(m.group(1) or "", {})[m.group(2)] = v
    return [out[k] for k in sorted(out, key=lambda s: (s != "", int(s) if s.isdigit() else 0))]


def light_rows(tags: dict[str, str]) -> tuple[list[tuple], str | None]:
    """A light's sector rows (from°, to°, Finnish colour code; an all-round light gives its
    colour as 0-360) and its character as charts print it, e.g. "Fl(2) WRG 10s"."""
    parts = _light_sectors(tags)
    if not parts:
        return [], None
    rows, colours = [], []
    char = group = period = None
    for p in parts:
        cols = [COLOURS[c] for c in re.split(r"[;,]", p.get("colour", "")) if c in COLOURS]
        for c in cols:
            if c not in colours:
                colours.append(c)
        a, b = _num(p.get("sector_start")), _num(p.get("sector_end"))
        if cols:
            if a is not None and b is not None:
                rows.append((a, b, cols[0]))
            elif not any(k.startswith("sector") for k in p):
                rows.append((0, 360, cols[0]))
        char = char or p.get("character")
        group = group or p.get("group")
        period = period or p.get("period")
    if not char:
        return rows, None
    text = char + (f"({group})" if group else "")
    letters = "".join(LETTERS[c] for c in ("v", "p", "vi", "k") if c in colours)
    if letters:
        text += " " + letters
    if period and _num(period) is not None:
        pv = _num(period)
        text += f" {int(pv) if pv == int(pv) else pv}s"
    return rows, text


# --- building a block -------------------------------------------------------------------

class BlockBuilder:
    """A block's foreign layers, from the depth model and OpenStreetMap elements fed in."""

    def __init__(self, bx: int, by: int, sampler: Sampler | None):
        self.bx, self.by = bx, by
        self.bounds = V.block_bounds(bx, by)
        minx, miny, maxx, maxy = self.bounds
        self.scale = V.UNITS / (maxx - minx)
        self.to_b = V.to_block(bx, by)
        self.n = GRID
        self.cell = (V.UNITS + 2 * GRID_MARGIN) / (GRID - 1)
        self.coast: CoastGrid | None = None
        self.builders: dict[str, V.LayerBuilder] = {}
        self.seen: set = set()
        m = V.UNITS * V.TILE_BUFFER / V.EXTENT
        self.box = (-m, -m, V.UNITS + m, V.UNITS + m)
        pad = m / self.scale
        self.pieces = convex_pieces((minx - pad, miny - pad, maxx + pad, maxy + pad), self.to_b)
        self.depth = array("d")  # NaN where the depth model has nothing
        if sampler is not None:
            self._sample(sampler)

    # grid nodes <-> block units
    def node_xy(self, i: float, j: float) -> tuple[float, float]:
        return -GRID_MARGIN + i * self.cell, -GRID_MARGIN + j * self.cell

    def _sample(self, sampler: Sampler) -> None:
        minx, _, _, maxy = self.bounds
        n = self.n
        xs = [minx + self.node_xy(i, 0)[0] / self.scale for i in range(n)]
        ys = [maxy - self.node_xy(0, j)[1] / self.scale for j in range(n)]
        lons = [V.mercator_to_lonlat(x, 0)[0] for x in xs]
        lats = [V.mercator_to_lonlat(0, y)[1] for y in ys]
        nan = math.nan
        depth = self.depth
        for j in range(n):
            for i in range(n):
                d = sampler.depth_m(xs[i], ys[j], lons[i], lats[j])
                depth.append(nan if d is None else d)

    def has_shore(self) -> bool:
        """Whether the depth model has both water and land here (worth asking for the coastline)."""
        wet = dry = False
        for d in self.depth:
            if d > 0:
                wet = True
            else:
                dry = True
            if wet and dry:
                return True
        return False

    def has_water(self) -> bool:
        return any(d > 0 for d in self.depth)

    def lonlat_box(self, margin_units: float) -> tuple[float, float, float, float]:
        """(south, west, north, east) of the block with a margin in block units."""
        minx, miny, maxx, maxy = self.bounds
        pad = margin_units / self.scale
        w, s = V.mercator_to_lonlat(minx - pad, miny - pad)
        e, n = V.mercator_to_lonlat(maxx + pad, maxy + pad)
        return s, w, n, e

    def _b(self, key: str) -> V.LayerBuilder:
        b = self.builders.get(key)
        if b is None:
            b = self.builders[key] = V.LayerBuilder(V.SOURCES[key][2] if key in V.SOURCES else "area")
        return b

    def _project(self, lon: float, lat: float) -> tuple[float, float]:
        return self.to_b(*V.lonlat_to_mercator(lon, lat))

    def _point_ok(self, key: str, lon: float, lat: float) -> tuple[float, float] | None:
        mx, my = V.lonlat_to_mercator(lon, lat)
        x, y = self.to_b(mx, my)
        pm = V.POINT_MARGINS.get(key, V.BLOCK_POINT_MARGIN)
        if not (-pm <= x <= V.UNITS + pm and -pm <= y <= V.UNITS + pm) or in_finland_m(mx, my):
            return None
        return x, y

    def add(self, el: dict) -> None:
        """One OpenStreetMap element."""
        tags = el.get("tags") or {}
        if el["type"] == "way" and not tags:
            self._coast(el.get("geometry") or [])
            return
        st = tags.get("seamark:type")
        if st:
            sig = (el["type"], st, el.get("lat"), el.get("lon"), len(el.get("geometry") or ()))
            if sig in self.seen:
                return
            self.seen.add(sig)
            if el["type"] == "node":
                self._mark(st, tags, el["lon"], el["lat"])
            else:
                self._routeing(st, tags, el)
        place = tags.get("place") or tags.get("natural")
        if place in NAME_CODES and tags.get("name"):
            pos = (el["lon"], el["lat"]) if "lat" in el else el.get("center")
            if pos:
                p = self._point_ok("name", *pos)
                if p:
                    self._b("name").point((tags["name"], "osm", NAME_CODES[place]), *p)

    def _coast(self, geometry: list) -> None:
        if len(geometry) < 2:
            return
        if self.coast is None:
            self.coast = CoastGrid(self.n)
        k = 1 / self.cell
        pts = []
        for lon, lat in geometry:
            x, y = self._project(lon, lat)
            pts.append(((x + GRID_MARGIN) * k, (y + GRID_MARGIN) * k))
        self.coast.add(pts)

    def _mark(self, st: str, tags: dict, lon: float, lat: float) -> None:
        if st == "rock":
            p = self._point_ok("rock", lon, lat)
            if p:
                self._b("rock").point((ROCK_CODES.get(tags.get("seamark:rock:water_level", ""), 38511),), *p)
            return
        if st == "wreck":
            p = self._point_ok("wreck", lon, lat)
            if p:
                self._b("wreck").point((WRECK_CODES.get(tags.get("seamark:wreck:category", ""), 16721),), *p)
            return
        if st == "pilot_boarding":
            p = self._point_ok("pilot", lon, lat)
            if p:
                self._b("pilot").point((tags.get("seamark:name") or tags.get("name"),), *p)
            return
        props = aid_props(tags)
        if props is None:
            return
        p = self._point_ok("aid", lon, lat)
        if not p:
            return
        self._b("aid").point(props, *p)
        sectors, text = light_rows(tags)
        for a, b, colour in sectors:
            self._b("sector").point((a, b, colour), *p)
        if text:
            self._b("light").point((text,), *p)

    def _rings(self, el: dict) -> list[list]:
        if el["type"] == "way":
            g = el.get("geometry") or []
            return [g] if len(g) >= 4 and g[0] == g[-1] else []
        outer = [m["geometry"] for m in el.get("members") or () if m.get("role") in ("outer", "") and m.get("geometry")]
        inner = [m["geometry"] for m in el.get("members") or () if m.get("role") == "inner" and m.get("geometry")]
        return join_ways(outer) + join_ways(inner)

    def _lines(self, el: dict) -> list[list]:
        if el["type"] == "way":
            return [el.get("geometry") or []]
        return [m["geometry"] for m in el.get("members") or () if m.get("geometry")]

    def _area(self, key: str, props: tuple, rings_ll: list[list]) -> None:
        rings = [[self._project(lon, lat) for lon, lat in ring] for ring in rings_ll]
        for piece in self.pieces:
            clipped = []
            for ring in rings:
                part = clip_convex(V.clip_ring(ring, *self.box), piece)
                if len(part) >= 3:
                    clipped.append(part)
            if clipped:
                self._b(key).area(props, clipped)

    def _line(self, key: str, props: tuple, lines_ll: list[list]) -> None:
        for line in lines_ll:
            pts = [self._project(lon, lat) for lon, lat in line]
            for part in V.clip_line(pts, *self.box):
                for piece in self.pieces:
                    for sub in clip_line_convex(part, piece):
                        self._b(key).line(props, sub)

    def _routeing(self, st: str, tags: dict, el: dict) -> None:
        if st in ("recommended_track", "navigation_line"):
            bearing = _num(tags.get(f"seamark:{st}:orientation"))
            self._line("lead", (bearing,), self._lines(el))
            return
        if st in TSS_LINES:
            self._line("tssline", (TSS_LINES[st],), self._lines(el))
            return
        kind = TSS_AREAS.get(st)
        if kind is None:
            return
        rings = self._rings(el)
        orient = _num(tags.get(f"seamark:{st}:orientation"))
        if rings:
            self._area("tss", (kind, orient), rings)
        elif st == "separation_lane":
            # A lane mapped as its centre line, in the direction of traffic: drawn as a band
            # either side of it, a piece for each stretch with its own direction.
            for line in self._lines(el):
                self._lane(line, orient)
        else:
            self._line("tssline", ("bnd",), self._lines(el))

    def _lane(self, line: list, orient: float | None) -> None:
        for (alon, alat), (blon, blat) in zip(line, line[1:]):
            ax, ay = V.lonlat_to_mercator(alon, alat)
            bx, by = V.lonlat_to_mercator(blon, blat)
            length = math.hypot(bx - ax, by - ay)
            if length == 0:
                continue
            half = LANE_HALF_M / math.cos(math.radians((alat + blat) / 2))
            nx, ny = -(by - ay) / length * half, (bx - ax) / length * half
            bearing = orient if orient is not None else math.degrees(math.atan2(bx - ax, by - ay)) % 360
            ring = [(ax + nx, ay + ny), (bx + nx, by + ny), (bx - nx, by - ny), (ax - nx, ay - ny)]
            ring_ll = [V.mercator_to_lonlat(x, y) for x, y in ring]
            self._area("tss", ("lane", round(bearing, 1)), [ring_ll + ring_ll[:1]])

    # --- depths ---

    def _masks(self) -> tuple[bytearray, bytearray]:
        """Nodes inside Finnish waters: exactly (for contours) and MASK_M in (for areas)."""
        n = self.n
        minx, _, _, maxy = self.bounds
        exact, deep = bytearray(n * n), bytearray(n * n)
        mm = MASK_M
        spans_cache: dict[float, list[float]] = {}

        def spans(y: float) -> list[float]:
            s = spans_cache.get(y)
            if s is None:
                s = spans_cache[y] = _poly_spans(FINNISH_M, y)
            return s

        for j in range(n):
            y = maxy - self.node_xy(0, j)[1] / self.scale
            row, up, down = spans(y), spans(y + mm), spans(y - mm)
            if not row and not up and not down:
                continue
            for i in range(n):
                x = minx + self.node_xy(i, 0)[0] / self.scale
                if _inside_spans(row, x):
                    exact[j * n + i] = 1
                    if (_inside_spans(row, x - mm) and _inside_spans(row, x + mm)
                            and _inside_spans(up, x) and _inside_spans(down, x)):
                        deep[j * n + i] = 1
        return exact, deep

    def _level_rings(self, region: list, level: float) -> list[list]:
        rings = []
        for ring in march(region, level, True):
            part = V.clip_ring(self._to_units(ring), *self.box)
            if len(part) >= 3:
                rings.append(part)
        return rings

    def _to_units(self, pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [self.node_xy(i, j) for i, j in pts]

    def depth_layers(self) -> None:
        n = self.n
        vals = classify(n, self.depth, self.coast)
        self.depth, self.coast = array("d"), None  # not needed past here
        exact, deep = self._masks()
        region = []
        for j in range(n):
            row = vals[j * n:(j + 1) * n]
            for i in range(n):
                if deep[j * n + i]:
                    row[i] = MASKED
            region.append(row)
        tol = self.cell * 0.2
        depth = self._b("depth")
        # Two levels' rings at a time (a band is the area above one less that above the next):
        # their vertices are the biggest use of RAM here.
        below = self._level_rings(region, BANDS[0][0])
        for d1, d2 in BANDS:
            above = self._level_rings(region, d2) if d2 < BANDS[-1][1] else []
            if below:
                rings = [_simplify_ring(r, tol) for r in band_rings(below, above, self.box)]
                depth.area((d1, d2), [r for r in rings if len(r) >= 3])
            below = above
        del region
        grid = [vals[j * n:(j + 1) * n] for j in range(n)]

        def skip(i: int, j: int) -> bool:
            return bool(exact[j * n + i] or exact[j * n + i + 1] or exact[(j + 1) * n + i]
                        or exact[(j + 1) * n + i + 1])

        top = max(vals)
        contour = self._b("contour")
        for level in V.CONTOURS:
            if level >= top:
                break
            for line in march(grid, level, False, skip):
                for part in V.clip_line(self._to_units(line), *self.box):
                    part = V.simplify(part, tol)
                    if len(part) >= 2:
                        contour.line((level,), part)

    def land_layer(self) -> None:
        """Land under the foreign water: FOREIGN in the block itself, so neighbours meet edge to edge."""
        ring = foreign_ring(self.bounds, self.to_b)
        if ring:
            ring = V.clip_ring(ring, 0, 0, V.UNITS, V.UNITS)
            if len(ring) >= 3:
                self._b("xland").area((), [ring])

    def finish(self, with_depths: bool) -> dict[str, V.Layer]:
        if with_depths:
            self.land_layer()
            if self.has_water():
                self.depth_layers()
        self.depth = array("d")
        self.coast = None
        return {k: b.layer for k, b in self.builders.items()}


# Layers of the foreign part drawn on overview tiles: the depths, contours and land there come
# from the depth model directly (see overview_rows), so they are left out of blocks' overviews.
OVERVIEW_KEYS = ("aid", "light", "sector", "name", "tss", "tssline", "pilot")


def overview_rows(z: int, x: int, y: int, sampler: Sampler) -> dict[str, list]:
    """Depth areas, contours and land of an overview tile (zooms 8-9) straight from the depth
    model, in the tile format (vchart._format)."""
    minx, miny, maxx, maxy = V._tile_mercator(z, x, y)
    k = V.EXTENT / (maxx - minx)
    lo, hi = -V.TILE_BUFFER, V.EXTENT + V.TILE_BUFFER
    n = OV_GRID
    step = (hi - lo) / (n - 1)
    xs = [minx + (lo + i * step) / k for i in range(n)]
    ys = [maxy - (lo + j * step) / k for j in range(n)]
    lons = [V.mercator_to_lonlat(mx, 0)[0] for mx in xs]
    lats = [V.mercator_to_lonlat(0, my)[1] for my in ys]
    rows, exact = [], []
    mm = MASK_M * 3  # zoomed out the overlap may be wider
    for j in range(n):
        sp, up, down = _poly_spans(FINNISH_M, ys[j]), _poly_spans(FINNISH_M, ys[j] + mm), _poly_spans(FINNISH_M, ys[j] - mm)
        row, ex = array("d"), bytearray(n)
        for i in range(n):
            inside = bool(sp) and _inside_spans(sp, xs[i])
            ex[i] = inside
            if inside and _inside_spans(sp, xs[i] - mm) and _inside_spans(sp, xs[i] + mm) \
                    and _inside_spans(up, xs[i]) and _inside_spans(down, xs[i]):
                row.append(MASKED)
                continue
            d = sampler.depth_m(xs[i], ys[j], lons[i], lats[j])
            row.append(LAND if d is None or d <= 0 else max(d, SHALLOWEST))
        rows.append(row)
        exact.append(ex)

    def units(pts):
        return [(lo + i * step, lo + j * step) for i, j in pts]

    out: dict[str, list] = {}
    rings_at = {}
    for level in sorted({b[0] for b in BANDS}):
        rings = []
        for ring in march(rows, level, True):
            part = V.clip_ring(units(ring), lo, lo, hi, hi)
            if len(part) >= 3:
                rings.append(part)
        rings_at[level] = rings
    depth = []
    for d1, d2 in BANDS:
        if rings_at[d1]:
            rings = [V._flat(_simplify_ring(r, 0.5))
                     for r in band_rings(rings_at[d1], rings_at.get(d2, []), (lo, lo, hi, hi))]
            rings = [r for r in rings if len(r) >= 6]
            if rings:
                depth.append([V._depth_band(d1, d2), *rings])
    if depth:
        out["depth"] = depth
    grid = rows
    for r in grid:
        for i, v in enumerate(r):
            if v == MASKED:
                r[i] = LAND

    def skip(i: int, j: int) -> bool:
        return exact[j][i] or exact[j][i + 1] or exact[j + 1][i] or exact[j + 1][i + 1]

    contours = []
    for level in V.OVERVIEW_CONTOURS:
        for line in march(grid, level, False, skip):
            for part in V.clip_line(units(line), lo, lo, hi, hi):
                flat = V._flat(V.simplify(part, 0.5))
                if len(flat) >= 4:
                    contours.append([level, flat])
    if contours:
        out["contour"] = contours
    land = foreign_ring((minx, miny, maxx, maxy), lambda mx, my: ((mx - minx) * k, (maxy - my) * k))
    if land:
        out["land"] = [V._flat(land)]
    return out


def merge_layers(a: V.Layer, b: V.Layer) -> V.Layer:
    """Two layers of the same kind as one (a block's Finnish and foreign rows)."""
    if not b.n:
        return a
    if not a.n:
        return b
    out = V.Layer(a.kind)
    out.props = list(a.props) + list(b.props)
    off = len(a.props)
    out.pi = array("i", a.pi)
    out.pi.extend(p + off for p in b.pi)
    out.co = array("i", a.co)
    out.co.extend(b.co)
    if a.kind != "point":
        out.bb = array("i", a.bb)
        out.bb.extend(b.bb)
        nv = len(a.co) // 2
        out.vo = array("i", a.vo)
        out.vo.extend(v + nv for v in b.vo[1:])
        if a.kind == "area":
            nr = len(a.vo) - 1
            out.rs = array("i", a.rs)
            out.rs.extend(r + nr for r in b.rs[1:])
    return out


def merge_into(layers: dict, got: dict) -> None:
    for key, layer in got.items():
        have = layers.get(key)
        layers[key] = merge_layers(have, layer) if isinstance(have, V.Layer) and have.kind == layer.kind else layer


# --- fetching ---------------------------------------------------------------------------

class ForeignSource:
    """Fetches and caches what the foreign chart is drawn from, for a VectorChart."""

    def __init__(self, chart: "V.VectorChart"):
        self.chart = chart
        fake = chart.fetch_layer
        # Stand-ins (the simulator, tests) hang off the chart's fetch_layer stand-in.
        self.fake_dtm = getattr(fake, "foreign_dtm", None) if fake is not None else None
        self.fake_osm = getattr(fake, "foreign_osm", None) if fake is not None else None
        self.enabled = (fake is None and chart.session is not None) or self.fake_dtm is not None
        self._grids: dict[tuple[int, int], Grid | None] = {}  # a few, in RAM while blocks are built
        self._fetching: dict[tuple[int, int], asyncio.Task] = {}
        self._failed: dict[Any, float] = {}
        self._last_overpass = 0.0
        self._overpass_lock = asyncio.Lock()
        self._building = asyncio.Semaphore(1)  # one block's grids in RAM at a time
        self._overviewing = asyncio.Semaphore(1)  # and one overview tile's
        self.errors: dict[str, str] = {}

    def forget(self) -> None:
        """Let go of the depth model grids in RAM (the chart isn't being looked at)."""
        self._grids.clear()

    @property
    def dir(self) -> Path:
        return self.chart.dir / "foreign"

    def _log(self, what: str, msg: str, *args: Any) -> None:
        self.chart._log_once(f"foreign {what}", msg, *args)

    # --- depth model ---

    def _grid_path(self, x: int, y: int) -> Path:
        return self.dir / f"dtm{DTM_Z}_{x}_{y}.bin.gz"

    async def grid(self, x: int, y: int) -> Grid | None:
        if (x, y) in self._grids:
            return self._grids[(x, y)]
        task = self._fetching.get((x, y))
        if task is None:
            task = asyncio.create_task(self._load_grid(x, y))
            self._fetching[(x, y)] = task
            task.add_done_callback(lambda _t, k=(x, y): self._fetching.pop(k, None))
        g = await asyncio.shield(task)
        if g is not None:
            self._grids[(x, y)] = g
            while len(self._grids) > 6:
                self._grids.pop(next(iter(self._grids)))
        return g

    async def _load_grid(self, x: int, y: int) -> Grid | None:
        path = self._grid_path(x, y)
        age = V._age(path)
        if age is not None and (age < DTM_MAX_AGE or time.time() - self._failed.get(("dtm", x, y), 0) < FAILED_RETRY):
            try:
                return await asyncio.to_thread(lambda: Grid.from_bytes(gzip.decompress(path.read_bytes())))
            except (OSError, ValueError, EOFError) as err:
                _LOGGER.warning("Depth model file %s unreadable, fetched again: %s", path.name, err)
        if time.time() - self._failed.get(("dtm", x, y), 0) < FAILED_RETRY:
            return None
        lon0, lat0, lon1, lat1 = dtm_tile_bounds(x, y)
        pad = 0.003  # a few cells past the tile, for bilinear sampling at its edge
        try:
            if self.fake_dtm is not None:
                body = await self.fake_dtm(lon0 - pad, lat0 - pad, lon1 + pad, lat1 + pad)
            else:
                async with self.chart._limit:
                    body = await self.chart._get_bytes(EMODNET_WCS, [
                        ("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
                        ("CoverageId", EMODNET_COVERAGE), ("format", "image/tiff"),
                        ("subset", f"Lat({lat0 - pad:.5f},{lat1 + pad:.5f})"),
                        ("subset", f"Long({lon0 - pad:.5f},{lon1 + pad:.5f})"),
                    ], "depth model")
            grid = await asyncio.to_thread(parse_geotiff, body)
            del body
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError, struct.error,
                zlib.error) as err:
            self._failed[("dtm", x, y)] = time.time()
            msg = self.chart._scrub(str(err) or type(err).__name__)[:300]
            self.errors["depth model"] = msg
            self._log("dtm", "EMODnet depth model for tile %s/%s/%s failed: %s", DTM_Z, x, y, msg)
            if age is not None:  # an old copy beats none
                try:
                    return await asyncio.to_thread(lambda: Grid.from_bytes(gzip.decompress(path.read_bytes())))
                except (OSError, ValueError, EOFError):
                    pass
            return None
        self.errors.pop("depth model", None)
        try:
            await asyncio.to_thread(_write_bytes, path, gzip.compress(grid.to_bytes(), 5))
        except OSError as err:
            _LOGGER.warning("Could not store the depth model: %s", err)
        return grid

    async def sampler(self, bounds: tuple[float, float, float, float]) -> Sampler | None:
        """The depth model for a web-mercator box, or None if some of it couldn't be had."""
        grids = {}
        for key in dtm_tiles_for(bounds):
            g = await self.grid(*key)
            if g is None:
                return None
            grids[key] = g
        return Sampler(grids)

    # --- OpenStreetMap ---

    async def _overpass(self, query: str, sink: Callable[[dict], None], what: str) -> None:
        """Stream an Overpass answer into ``sink`` element by element (in a worker thread)."""
        if self.fake_osm is not None:
            for el in await self.fake_osm(query):
                sink(el)
            return
        async with self._overpass_lock:
            wait = OVERPASS_PAUSE - (time.monotonic() - self._last_overpass)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                stream = OsmStream(sink)
                size = 0
                async with self.chart.session.post(OVERPASS, data={"data": query}, headers={"User-Agent": USER_AGENT},
                                                   timeout=aiohttp.ClientTimeout(total=OVERPASS_TIMEOUT)) as resp:
                    if resp.status != 200:
                        text = (await resp.content.read(600)).decode("utf-8", "replace")
                        raise V._HTTPError(resp.status, re.sub(r"<[^>]+>", " ", text).strip())
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        size += len(chunk)
                        if size > OVERPASS_MAX:
                            raise ValueError(f"more than {OVERPASS_MAX >> 20} MB of {what}")
                        await asyncio.to_thread(stream.feed, chunk)
                await asyncio.to_thread(stream.close)
                if stream.remark and "error" in stream.remark.lower():
                    raise ValueError(stream.remark[:200])  # Overpass ran out of time or memory
            except Exception as err:  # noqa: BLE001 - the XML parser raises its own kind
                if isinstance(err, (aiohttp.ClientError, asyncio.TimeoutError, ValueError)):
                    raise
                raise ValueError(f"{type(err).__name__}: {err}") from err
            finally:
                self._last_overpass = time.monotonic()

    # --- a block part ---

    async def fetch_part(self, bx: int, by: int) -> bool:
        """Build and write a block's foreign part. False when the depth model couldn't be had
        (tried again later); without OpenStreetMap the part is written from the depth model
        alone and marked missing, so it's fetched again sooner."""
        chart = self.chart
        bounds = V.block_bounds(bx, by)
        path = chart._path(bx, by, PART)
        started = time.monotonic()
        missing: list[str] = []
        if not self.enabled or not wanted(bounds):
            built: dict[str, V.Layer] = {}
            with_depths = False
        else:
            async with self._building:
                if chart._stale((bx, by)):
                    raise V._Skipped  # the map moved on while this block waited its turn
                built, missing = await self._build(bx, by, bounds)
            if built is None:
                return False
            with_depths = True

        def write() -> None:
            levels = V.make_levels(built)
            overview = {k: L for k, L in V.make_overview(built).items() if k in OVERVIEW_KEYS}
            V.write_block(path, {"v": 2, "missing": missing, "absent": []}, levels, overview)

        await asyncio.to_thread(write)
        chart._missing[path.name] = (path.stat().st_mtime, bool(missing), ())
        if with_depths:
            _LOGGER.info("Chart block %s,%s (outside Finland): %s in %.1f s", bx, by,
                         ", ".join(f"{L.n} {k}" for k, L in built.items()) or "nothing", time.monotonic() - started)
        return True

    async def _build(self, bx: int, by: int, bounds: tuple) -> tuple[dict | None, list[str]]:
        missing: list[str] = []
        pad = GRID_MARGIN / (V.UNITS / (bounds[2] - bounds[0]))
        sampler = await self.sampler((bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad))
        if sampler is None:
            return None, missing
        b = await asyncio.to_thread(BlockBuilder, bx, by, sampler)
        del sampler
        try:
            if b.has_shore():
                s, w, n, e = b.lonlat_box(GRID_MARGIN)
                await self._overpass(overpass_coast_query(s, w, n, e), b.add, "coastline")
            pts = b.lonlat_box(V.BLOCK_POINT_MARGIN)
            lines = b.lonlat_box(V.UNITS * V.TILE_BUFFER / V.EXTENT)
            await self._overpass(overpass_marks_query(pts, lines), b.add, "sea marks")
            self.errors.pop("openstreetmap", None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            msg = str(err) or type(err).__name__
            self.errors["openstreetmap"] = msg[:300]
            self._log("osm", "OpenStreetMap coast and sea marks for block %s,%s failed: %s", bx, by, msg[:300])
            missing.append("osm")
            b.coast = None  # a coastline half read is worse than the depth model's own
            b.builders = {}
        built = await asyncio.to_thread(b.finish, True)
        del b
        return built, missing

    # --- overview tiles ---

    async def overview(self, z: int, x: int, y: int) -> tuple[dict[str, list], bool]:
        """Depth areas, contours and land for an overview tile, and whether all of it was there."""
        if not self.enabled:
            return {}, True
        bounds = V._tile_mercator(z, x, y)
        size = bounds[2] - bounds[0]
        pad = size * V.TILE_BUFFER / V.EXTENT
        padded = (bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad)
        if not wanted(padded):
            return {}, True
        async with self._overviewing:
            sampler = await self.sampler(padded)
            if sampler is None:
                return {}, False
            rows = await asyncio.to_thread(overview_rows, z, x, y, sampler)
            del sampler
        return rows, True


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def add_overview(tile: dict, rows: dict[str, list]) -> None:
    """Foreign overview rows into a cut overview tile."""
    for key in ("depth", "contour", "land"):
        if rows.get(key):
            tile[key] = (tile.get(key) or []) + rows[key]
    if rows.get("depth"):
        tile["depth"].sort(key=lambda row: -row[0])
