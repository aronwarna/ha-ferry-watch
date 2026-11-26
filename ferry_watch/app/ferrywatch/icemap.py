"""The Baltic ice chart as the ice analysts draw it: SMHI's open "analysis map sea ice"
(the joint Swedish-Finnish ice chart, the same one FMI publishes as a PDF), one zipped
shapefile a day since 2017.

Each area of it has the total concentration in tenths (iceact, as on the chart: 01 open
water, 10-90, 91 for 9+/10, 92 for 10/10, 98 ice free), the ice type (fast ice, level ice,
new ice, rotten ice ...), how deformed it is (rafted, slightly / moderately / heavily
ridged, brash barrier), and the thickness (smallest, usual, largest, cm). That is what
the raster ice chart (icechart.py, from Copernicus) is made from, so it is shown in the
same WMO colours, but sharp, with the ridged and rafted ice marked and the thickness
written on each area.

Days come from the download service's own listing (year -> month -> day), the shapefile
of a day is read once and kept as a small gzipped JSON with its outlines simplified for
the zoomed-out tiles. Tiles are drawn with Pillow (icechart.IceChart decides which source
draws a day); the panel cuts them to the drawn chart's water like the other sea layers.

Data: SMHI open data, CC BY 4.0 (made using E.U. Copernicus Marine Service information).
"""

from __future__ import annotations

import asyncio
import calendar
import gzip
import io
import json
import logging
import math
import struct
import time
import zipfile
from array import array
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from .config import USER_AGENT

_LOGGER = logging.getLogger(__name__)

BASE = "https://opendata-download-icemap.smhi.se/api/version/latest"
ATTRIBUTION = "Ice chart: SMHI and FMI ice services (SMHI open data, CC BY 4.0)"
TIMEOUT = 60
FILE_REV = 1  # bump when what's kept of a day changes: kept days are read again
KEEP_DAYS = 40
LIST_REFRESH = 3 * 3600  # s the listing of the current month is reused

# Outline detail kept: full (closer in) and simplified for zoomed-out tiles (degrees).
SIMPLE_EPS = 0.006
SIMPLE_BELOW_Z = 9

Get = Callable[[str], Awaitable[bytes]]

# --- what the codes mean -------------------------------------------------------------------

# Total concentration (iceact): code -> (tenths from, name, short)
CONC = {
    1: (0, "Open water", "<1/10"),
    10: (1, "Very open drift ice", "1/10"), 20: (2, "Very open drift ice", "2/10"),
    30: (3, "Very open drift ice", "3/10"), 40: (4, "Open drift ice", "4/10"),
    50: (5, "Open drift ice", "5/10"), 60: (6, "Open drift ice", "6/10"),
    70: (7, "Close drift ice", "7/10"), 80: (8, "Close drift ice", "8/10"),
    90: (9, "Very close drift ice", "9/10"), 91: (9.5, "Very close drift ice", "9+/10"),
    92: (10, "Compact ice", "10/10"),
}
ICE_FREE = 98

# Ice type (WMO), as SMHI lists them.
TYPES = {
    1: "Open water", 2: "Very open drift ice", 3: "Open drift ice", 4: "Close drift ice",
    5: "Very close drift ice", 6: "Consolidated ice", 7: "Level ice", 8: "Fast ice",
    9: "New ice", 10: "Rotten ice",
}
FAST = 8

# Deformation.
DEFORM = {1: "Rafted ice", 2: "Slightly ridged ice", 3: "Moderately ridged ice",
          4: "Heavily ridged ice", 5: "Brash ice barrier"}

# WMO colour code for total concentration (as icechart.CLASSES), fast ice grey.
COLOURS = [
    # The chart colours (WMO), a little softened so they sit well on the map.
    (1, (165, 208, 250)),     # under 1/10: open water
    (10, (150, 232, 170)),    # 1-3/10
    (40, (250, 226, 70)),     # 4-6/10
    (70, (246, 142, 48)),     # 7-8/10
    (90, (222, 52, 52)),      # 9-10/10
]
FAST_COLOUR = (160, 165, 175)


def colour_of(conc: int, kind: int) -> tuple[int, int, int] | None:
    if conc == ICE_FREE or conc <= 0:
        return None
    if kind == FAST:
        return FAST_COLOUR
    found = None
    for lo, rgb in COLOURS:
        if conc >= lo:
            found = rgb
    return found


def class_index(conc: int, kind: int) -> int | None:
    """Index into icechart.CLASSES (the legend), None for ice free."""
    if conc == ICE_FREE or conc <= 0:
        return None
    if kind == FAST:
        return 5
    tenths = CONC.get(conc, (conc / 10,))[0]
    return 0 if tenths < 1 else 1 if tenths < 4 else 2 if tenths < 7 else 3 if tenths < 9 else 4


# --- the shapefile -------------------------------------------------------------------------

def read_dbf(body: bytes) -> list[dict[str, str]]:
    n, head, size = struct.unpack("<IHH", body[4:12])
    fields = []
    for off in range(32, head - 1, 32):
        name = body[off:off + 11].split(b"\0")[0].decode("latin-1")
        fields.append((name, body[off + 16]))
    rows = []
    for k in range(n):
        o = head + k * size + 1
        row = {}
        for name, length in fields:
            row[name] = body[o:o + length].decode("latin-1").strip()
            o += length
        rows.append(row)
    return rows


def read_shp(body: bytes) -> list[list[list[tuple[float, float]]]]:
    """Polygon records as lists of rings of (lon, lat); a null shape is an empty list."""
    out = []
    o = 100
    while o + 8 <= len(body):
        length = struct.unpack(">i", body[o + 4:o + 8])[0] * 2
        rec = body[o + 8:o + 8 + length]
        o += 8 + length
        if len(rec) < 4 or struct.unpack("<i", rec[:4])[0] not in (5, 15, 25):
            out.append([])
            continue
        nparts, npts = struct.unpack("<ii", rec[36:44])
        starts = list(struct.unpack(f"<{nparts}i", rec[44:44 + 4 * nparts])) + [npts]
        xy = struct.unpack(f"<{2 * npts}d", rec[44 + 4 * nparts:44 + 4 * nparts + 16 * npts])
        rings = []
        for a, b in zip(starts, starts[1:]):
            rings.append([(xy[2 * i], xy[2 * i + 1]) for i in range(a, b)])
        out.append(rings)
    return out


def _num(text: str) -> int:
    try:
        return int(float(text))
    except ValueError:
        return 0


def simplify(ring: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """Douglas-Peucker on a closed ring (kept closed); rings that shrink away give []."""
    n = len(ring)
    if n < 5:
        return ring
    keep = bytearray(n)
    keep[0] = keep[-1] = 1
    # A closed ring: split at its farthest point from the start, so each half has two ends.
    x0, y0 = ring[0]
    far = max(range(n), key=lambda i: (ring[i][0] - x0) ** 2 + (ring[i][1] - y0) ** 2)
    keep[far] = 1
    stack = [(0, far), (far, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = ring[a]
        bx, by = ring[b]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        best, at = 0.0, -1
        for i in range(a + 1, b):
            px, py = ring[i]
            d = abs(dy * px - dx * py + bx * ay - by * ax) / length if length > 1e-12 else math.hypot(px - ax, py - ay)
            if d > best:
                best, at = d, i
        if best > eps:
            keep[at] = 1
            stack += [(a, at), (at, b)]
    out = [p for p, k in zip(ring, keep) if k]
    return out if len(out) >= 4 else []


def ring_area(ring: list[tuple[float, float]]) -> float:
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:]))


def label_point(rings: list[list[tuple[float, float]]]) -> tuple[float, float] | None:
    """A point well inside the area: the middle of the widest span across its outer ring
    on a few horizontal lines, outside its holes."""
    outer = max(rings, key=lambda r: abs(ring_area(r)), default=None)
    if not outer or len(outer) < 4:
        return None
    ys = [p[1] for p in outer]
    lo, hi = min(ys), max(ys)
    best = None
    for k in range(1, 8):
        y = lo + (hi - lo) * k / 8
        xs = []
        for ring in rings:
            for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
                if (y1 <= y < y2) or (y2 <= y < y1):
                    xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        xs.sort()
        for a, b in zip(xs[0::2], xs[1::2]):
            w = (b - a) * math.cos(math.radians(y))
            if best is None or w > best[0]:
                best = (w, (a + b) / 2, y)
    return (best[1], best[2]) if best else None


def parse_zip(body: bytes) -> dict[str, Any]:
    """A day's zip -> what's kept: {"day", "areas": [{c, t, d, mn, av, mx, box, lab, r, s}]},
    rings as flat [lon, lat, ...] lists rounded to ~1 m, `s` the simplified ones."""
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        names = z.namelist()
        shp = z.read(next(n for n in names if n.lower().endswith(".shp")))
        dbf = z.read(next(n for n in names if n.lower().endswith(".dbf")))
    rows = read_dbf(dbf)
    shapes = read_shp(shp)
    areas = []
    day = ""
    for row, rings in zip(rows, shapes):
        day = day or row.get("chartdate", "")
        conc = _num(row.get("iceact", "0"))
        if not rings or conc == ICE_FREE or conc <= 0:
            continue
        rings = [r for r in rings if len(r) >= 4]
        if not rings:
            continue
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        simple = [s for s in (simplify(r, SIMPLE_EPS) for r in rings) if s]
        lab = label_point(rings)
        areas.append({
            "c": conc, "t": _num(row.get("type", "0")), "d": max(0, _num(row.get("deformatio", "0"))),
            "mn": _num(row.get("icemin", "0")), "av": _num(row.get("icetck", "0")), "mx": _num(row.get("icemax", "0")),
            "box": [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)],
            "lab": [round(lab[0], 4), round(lab[1], 4)] if lab else None,
            "r": [[round(v, 5) for p in r for v in p] for r in rings],
            "s": [[round(v, 5) for p in r for v in p] for r in simple],
        })
    return {"rev": FILE_REV, "day": day, "areas": areas}


# --- a day, ready to draw ------------------------------------------------------------------

class Day:
    """One day's areas, outlines as arrays (compact in memory)."""

    def __init__(self, data: dict[str, Any]):
        self.day = data.get("day", "")
        self.areas = []
        for a in data.get("areas", []):
            a = dict(a)
            a["r"] = [array("d", r) for r in a["r"]]
            a["s"] = [array("d", r) for r in a.get("s") or []]
            self.areas.append(a)
        self.ice = [a for a in self.areas if class_index(a["c"], a["t"]) not in (None, 0)]

    @property
    def has_ice(self) -> bool:
        return bool(self.ice)

    def thickest(self) -> int:
        return max((a["mx"] for a in self.ice), default=0)

    def at(self, lat: float, lon: float) -> dict[str, Any] | None:
        """The area at a place (the last drawn, so the top one)."""
        for a in reversed(self.areas):
            x0, y0, x1, y1 = a["box"]
            if not (x0 <= lon <= x1 and y0 <= lat <= y1):
                continue
            inside = False
            for r in a["r"]:
                n = len(r) // 2
                j = n - 1
                for i in range(n):
                    xi, yi, xj, yj = r[2 * i], r[2 * i + 1], r[2 * j], r[2 * j + 1]
                    if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                        inside = not inside
                    j = i
            if inside:
                return a
        return None


def describe(a: dict[str, Any], day: str) -> dict[str, Any]:
    """What a tap says about an area."""
    conc = CONC.get(a["c"])
    idx = class_index(a["c"], a["t"])
    kind = TYPES.get(a["t"], "")
    name = kind if a["t"] in (FAST, 6, 7, 9, 10) else (conc[1] if conc else kind)
    out = {"day": day, "class": name, "short": conc[2] if conc else "", "conc_code": a["c"],
           "tenths": conc[0] if conc else None, "type": kind, "deform": DEFORM.get(a["d"], ""),
           "min": a["mn"], "thick": a["av"], "max": a["mx"], "src": "chart"}
    if idx is None:
        out["class"], out["short"] = "No ice", ""
    # As the raster's taps: concentration in % for the panel's line.
    out["conc"] = round((conc[0] if conc else 0) * 10)
    return out


# --- drawing -------------------------------------------------------------------------------

OUT = 512
ALPHA = 235
EDGE = (40, 40, 40, 150)
LABEL_Z = 7
LABEL_MIN_PX = 70  # an area this wide on the tile (px of 512) gets its thickness written
TILE = 256


def _world(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = TILE * 2 ** z
    s = math.sin(math.radians(max(-85.0, min(85.0, lat))))
    return (lon + 180) / 360 * n, (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n


def _lonlat_box(z: int, x: int, y: int, pad: float = 0.0) -> tuple[float, float, float, float]:
    n = 2 ** z

    def lat(yy: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return (x / n * 360 - 180 - pad, lat(y + 1) - pad, (x + 1) / n * 360 - 180 + pad, lat(y) + pad)


def _hatch(size: int, kind: int, scale: float) -> Any:
    """A see-through RGBA pattern marking deformed ice (WMO-like symbols, dark)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ink = (25, 25, 25, 200)
    w = max(1, round(1.4 * scale))
    if kind == 1:  # rafted: short slanted strokes in pairs
        step = round(26 * scale)
        for yy in range(0, size + step, step):
            for xx in range((yy // step) % 2 * step // 2, size + step, step):
                d.line((xx, yy, xx + 7 * scale, yy - 7 * scale), fill=ink, width=w)
        return img
    if kind == 5:  # brash barrier: crosses
        step = round(22 * scale)
        for yy in range(0, size + step, step):
            for xx in range(0, size + step, step):
                r = 4 * scale
                d.line((xx - r, yy - r, xx + r, yy + r), fill=ink, width=w)
                d.line((xx - r, yy + r, xx + r, yy - r), fill=ink, width=w)
        return img
    # Ridged: little roofs, closer together the more ridged.
    step = round({2: 34, 3: 24, 4: 16}.get(kind, 24) * scale)
    for row, yy in enumerate(range(0, size + step, step)):
        for xx in range(row % 2 * step // 2, size + step, step):
            r = 5 * scale
            d.line((xx - r, yy + r * 0.6, xx, yy - r * 0.6, xx + r, yy + r * 0.6), fill=ink, width=w, joint="curve")
    return img


_FONT: dict[int, Any] = {}


def _font(px: int) -> Any:
    from PIL import ImageFont

    if px not in _FONT:
        try:
            _FONT[px] = ImageFont.load_default(size=px)
        except Exception:  # noqa: BLE001 - Pillow without FreeType
            _FONT[px] = ImageFont.load_default()
    return _FONT[px]


def label_text(a: dict[str, Any]) -> str:
    return f"{a['mn']}-{a['mx']}" if a["mn"] and a["mn"] != a["mx"] else f"{a['mx']}"


def labels_near(day: Day, z: int, x: int, y: int) -> list[tuple[float, float, str]]:
    """Thickness labels (world px at zoom z) that fall on or near a tile."""
    ox, oy = x * TILE, y * TILE
    w, h = LABEL_BOX
    return [(lx, ly, text) for lx, ly, text in placed_labels(day, z)
            if ox - w <= lx <= ox + TILE + w and oy - h <= ly <= oy + TILE + h]


def placed_labels(day: Day, z: int) -> list[tuple[float, float, str]]:
    """All the day's thickness labels at a zoom, placed once for the whole chart (so tiles
    agree at their seams): bigger areas first, and a label that would touch one already
    placed is left out."""
    cache = day.__dict__.setdefault("_labels", {})
    if z in cache:
        return cache[z]
    cand = []
    for a in day.areas:
        if not a.get("lab") or a["mx"] <= 0 or colour_of(a["c"], a["t"]) is None:
            continue
        x0, _ = _world(a["box"][0], a["box"][1], z)
        x1, _ = _world(a["box"][2], a["box"][3], z)
        if x1 - x0 < LABEL_MIN_PX * TILE / OUT:
            continue
        lx, ly = _world(a["lab"][0], a["lab"][1], z)
        cand.append((-(x1 - x0), lx, ly, label_text(a)))
    cand.sort()
    out: list[tuple[float, float, str]] = []
    w, h = LABEL_BOX
    for _, lx, ly, text in cand:
        if not any(abs(lx - px) < w and abs(ly - py) < h for px, py, _ in out):
            out.append((lx, ly, text))
    cache[z] = out
    return out


LABEL_BOX = (46.0, 17.0)  # a label's room in world px (256 tiles): no other label nearer


def draw_tile(day: Day, z: int, x: int, y: int, size: int = OUT) -> bytes | None:
    """One map tile of the day's chart, or None where it has no ice."""
    from PIL import Image, ImageChops, ImageDraw

    w0, s0, e0, n0 = _lonlat_box(z, x, y)
    pw, ps, pe, pn = _lonlat_box(z, x, y, pad=(e0 - w0) * 0.05)
    k = size / TILE
    ox, oy = x * TILE, y * TILE
    simple = z < SIMPLE_BELOW_Z
    hit = [a for a in day.areas if a["box"][0] <= pe and a["box"][2] >= pw and a["box"][1] <= pn and a["box"][3] >= ps]
    hit = [a for a in hit if colour_of(a["c"], a["t"]) is not None]
    if not hit:
        return None
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    edges = Image.new("L", (size, size), 0)
    hatch: dict[int, Any] = {}
    scale = k * (0.75 if z <= 7 else 1.0)
    drew = False
    for a in hit:
        rings = a["s"] if simple and a["s"] else a["r"]
        polys = []
        for r in rings:
            pts = []
            for i in range(0, len(r), 2):
                wx, wy = _world(r[i], r[i + 1], z)
                pts.append(((wx - ox) * k, (wy - oy) * k))
            if len(pts) >= 3:
                polys.append(pts)
        if not polys:
            continue
        # Only the part of the tile the area covers is worked on.
        bx0 = max(0, int(min(p[0] for pts in polys for p in pts)) - 1)
        by0 = max(0, int(min(p[1] for pts in polys for p in pts)) - 1)
        bx1 = min(size, int(max(p[0] for pts in polys for p in pts)) + 2)
        by1 = min(size, int(max(p[1] for pts in polys for p in pts)) + 2)
        if bx1 <= bx0 or by1 <= by0:
            continue
        bw, bh = bx1 - bx0, by1 - by0
        mask = Image.new("1", (bw, bh), 0)
        ed = ImageDraw.Draw(edges)
        for pts in polys:
            local = [(px - bx0, py - by0) for px, py in pts]
            ring = Image.new("1", (bw, bh), 0)
            ImageDraw.Draw(ring).polygon(local, fill=1)
            mask = ImageChops.logical_xor(mask, ring)  # even-odd: holes come out
            ed.line(pts + [pts[0]], fill=255, width=max(1, round(k * 0.8)))
        if not mask.getbbox():
            continue
        drew = True
        m = mask.convert("L")
        rgb = colour_of(a["c"], a["t"])
        img.paste(Image.new("RGBA", (bw, bh), (*rgb, ALPHA)), (bx0, by0), m)
        if a["d"] in DEFORM and z >= 6:
            pat = hatch.get(a["d"])
            if pat is None:
                pat = hatch[a["d"]] = _hatch(size, a["d"], scale)
            part = pat.crop((bx0, by0, bx1, by1))
            layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
            layer.paste(part, (0, 0), ImageChops.multiply(m, part.getchannel("A")))
            img.alpha_composite(layer, (bx0, by0))
    if not drew:
        return None
    # Thin dark lines round the areas, as the analysts draw them.
    alpha = img.getchannel("A").point(lambda v: 255 if v else 0)
    edges = ImageChops.multiply(edges, alpha).point(lambda v: EDGE[3] if v else 0)
    img.alpha_composite(Image.merge("RGBA", (*[Image.new("L", img.size, EDGE[0])] * 3, edges)))
    draw = ImageDraw.Draw(img)
    font = _font(max(13, round(size / 19)))
    for px, py, text in labels_near(day, z, x, y) if z >= LABEL_Z else []:
        draw.text(((px - ox) * k, (py - oy) * k), text, font=font, fill=(17, 24, 39, 255), anchor="mm",
                  stroke_width=max(2, size // 200), stroke_fill=(255, 255, 255, 235))
    out = io.BytesIO()
    img.save(out, "PNG", compress_level=6)
    return out.getvalue()


# --- the source ----------------------------------------------------------------------------

class IceMaps:
    """SMHI's daily ice charts: the days there are, each read once and kept on disk."""

    def __init__(self, directory: Path, session: aiohttp.ClientSession | None, get: Get | None = None):
        self.dir = directory
        self.session = session
        self.get = get or self._get
        self._months: dict[str, tuple[float, list[int]]] = {}  # "YYYY/M" -> (read at, days)
        self._open: dict[int, Day] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self.failed: dict[int, tuple[float, Exception]] = {}
        self.listing_error: str | None = None

    async def _get(self, url: str) -> bytes:
        if self.session is None:
            raise ValueError("no connection")
        async with self.session.get(url, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            if resp.status == 404:
                return b""
            resp.raise_for_status()
            return await resp.read()

    async def _json(self, url: str) -> dict[str, Any]:
        body = await self.get(url)
        return json.loads(body) if body else {}

    async def month_days(self, year: int, month: int, now: float) -> list[int]:
        key = f"{year}/{month}"
        cur = time.gmtime(now)
        recent = (year, month) == (cur.tm_year, cur.tm_mon)
        hit = self._months.get(key)
        if hit and (not recent or now - hit[0] < LIST_REFRESH):
            return hit[1]
        data = await self._json(f"{BASE}/year/{year}/month/{month}.json")
        days = sorted(calendar.timegm((year, month, int(d["key"]), 0, 0, 0, 0, 0, 0))
                      for d in data.get("day", []) if str(d.get("key", "")).isdigit())
        self._months[key] = (now, days)
        return days

    async def days(self, start: int, now: float) -> list[int]:
        """The days there are from `start` (a 1 November) to now, ice season months only."""
        out: list[int] = []
        t = time.gmtime(start)
        year, month = t.tm_year, t.tm_mon
        cur = time.gmtime(now)
        errors = []
        while (year, month) <= (cur.tm_year, cur.tm_mon):
            if month in (11, 12, 1, 2, 3, 4, 5):
                try:
                    out += await self.month_days(year, month, now)
                except Exception as err:  # noqa: BLE001 - a month missing leaves a gap
                    errors.append(f"{type(err).__name__}: {err}" if str(err) else type(err).__name__)
            month += 1
            if month > 12:
                year, month = year + 1, 1
        self.listing_error = errors[0] if errors else None
        if errors:
            _LOGGER.warning("SMHI ice charts: %d months could not be listed (%s)", len(errors), errors[0])
        return sorted(d for d in out if d <= now)

    def _path(self, day: int) -> Path:
        return self.dir / f"{time.strftime('%Y-%m-%d', time.gmtime(day))}.json.gz"

    def kept(self, day: int) -> bool:
        return self._path(day).exists()

    def summary(self, day: int) -> list[float] | None:
        """[share of the Baltic with ice, thickest cm, ice km²] of a day read before."""
        return self.summaries().get(str(day))

    def summaries(self) -> dict[str, list[float]]:
        try:
            return json.loads((self.dir / "summary.json").read_text())
        except (OSError, ValueError):
            return {}

    def _note_summary(self, day: int, d: Day) -> None:
        path = self.dir / "summary.json"
        meta = self.summaries()
        path.parent.mkdir(parents=True, exist_ok=True)
        km2 = ice_km2(d)
        meta[str(day)] = [round(km2 / BALTIC_KM2, 4), d.thickest(), round(km2, -2)]
        path.write_text(json.dumps(meta))

    async def ready(self, day: int) -> Day | None:
        """The day, read from SMHI the first time; raises when it can't be read."""
        hit = self._open.get(day)
        if hit:
            return hit
        lock = self._locks.setdefault(day, asyncio.Lock())
        async with lock:
            hit = self._open.get(day)
            if hit:
                return hit
            path = self._path(day)
            data = await asyncio.to_thread(_read_kept, path)
            if data is None:
                when, why = self.failed.get(day, (0.0, None))
                if why is not None and time.time() - when < 120:
                    raise why
                try:
                    data = await self._fetch(day)
                except Exception as err:
                    self.failed[day] = (time.time(), err)
                    raise
                self.failed.pop(day, None)
            d = await asyncio.to_thread(Day, data)
            if self.summary(day) is None:
                await asyncio.to_thread(self._note_summary, day, d)
            self._open[day] = d
            while len(self._open) > 3:
                self._open.pop(next(iter(self._open)))
            return d

    async def _fetch(self, day: int) -> dict[str, Any]:
        t = time.gmtime(day)
        listing = await self._json(f"{BASE}/year/{t.tm_year}/month/{t.tm_mon}/day/{t.tm_mday}.json")
        links = [lk["href"] for item in listing.get("data", []) for lk in item.get("link", [])
                 if str(lk.get("href", "")).endswith(".zip")]
        if not links:
            raise ValueError(f"no ice chart for {time.strftime('%Y-%m-%d', t)}")
        body = await self.get(sorted(links)[-1])
        if not body:
            raise ValueError(f"the ice chart for {time.strftime('%Y-%m-%d', t)} came back empty")
        data = await asyncio.to_thread(parse_zip, body)
        await asyncio.to_thread(_write_kept, self._path(day), data)
        await asyncio.to_thread(self.prune)
        _LOGGER.info("SMHI ice chart for %s read: %d areas", data.get("day"), len(data["areas"]))
        return data

    def prune(self) -> None:
        try:
            files = sorted(p for p in self.dir.glob("*.json.gz"))
        except OSError:
            return
        for old in files[:-KEEP_DAYS]:
            old.unlink(missing_ok=True)


BALTIC_KM2 = 422_000  # the Baltic Sea with the Kattegat, as the ice services count it


def ice_km2(d: Day) -> float:
    """The ice's extent (km²): its areas' size from their outlines (holes taken out), the
    way the ice services give it, open water and ice free left out."""
    km2 = 0.0
    for a in d.ice:
        for r in a["r"]:
            pts = [(r[j], r[j + 1]) for j in range(0, len(r), 2)]
            lat = sum(p[1] for p in pts) / len(pts)
            signed = ring_area(pts) * 111.32 * 111.32 * math.cos(math.radians(lat))
            km2 += -signed  # outer rings run clockwise (negative), holes the other way
    return max(0.0, km2)


def _read_kept(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(gzip.decompress(path.read_bytes()))
    except (OSError, ValueError, EOFError):
        return None
    return data if data.get("rev") == FILE_REV else None


def _write_kept(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), 6))
    tmp.replace(path)
