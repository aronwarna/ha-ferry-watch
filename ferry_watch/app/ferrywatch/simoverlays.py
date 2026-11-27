"""Test overlays for the simulator, so the overlay switches, opacity and time bar work offline.

The rain radar runs with its real settings, its frames small PNGs (written here with zlib
and struct) of rain cells drifting east across the Gulf of Finland.
It goes through the real tile cache, with a session that draws the tiles instead of
fetching them. The wind forecast (a points overlay) is registered with the stations, its
FMI answers made up in simsea.py.
"""

from __future__ import annotations

import io
import json
import math
import struct
import zlib
from typing import Any

from . import radar
from .overlays import Overlays, every

TILE_PX = 64  # drawn small; the panel stretches it over its 256-pixel tile
# Rain cells: lat, lon at the reference time, radius in degrees of latitude, strength.
CELLS = [(59.75, 23.2, 0.22, 1.0), (60.05, 24.3, 0.16, 0.8), (59.55, 25.1, 0.28, 1.2),
         (59.9, 26.4, 0.2, 0.9), (60.3, 22.4, 0.25, 0.7), (59.35, 27.3, 0.18, 1.1)]
DRIFT = 0.9  # degrees of longitude an hour, eastwards (fast, so an hour's frames show it)
# Radar-like colours by strength: light blue, blue, green, yellow, red.
RAMP = [(0.15, (140, 200, 255)), (0.35, (40, 120, 240)), (0.6, (30, 180, 80)),
        (0.85, (250, 210, 40)), (1.1, (230, 60, 40))]


def png(width: int, height: int, rgba: bytes) -> bytes:
    """An RGBA PNG, written by hand (no Pillow needed)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    stride = width * 4
    raw = b"".join(b"\0" + rgba[r * stride:(r + 1) * stride] for r in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _colour(v: float) -> tuple[int, int, int, int]:
    if v < RAMP[0][0]:
        return 0, 0, 0, 0
    rgb = RAMP[0][1]
    for edge, c in RAMP:
        if v >= edge:
            rgb = c
    return (*rgb, min(230, int(120 + v * 110)))


def rain_tile(ts: int, z: int, x: int, y: int) -> bytes:
    hours = (ts % 86400) / 3600
    n = 2**z
    pixels = bytearray()
    for row in range(TILE_PX):
        merc = math.pi * (1 - 2 * (y + (row + 0.5) / TILE_PX) / n)
        lat = math.degrees(math.atan(math.sinh(merc)))
        k = math.cos(math.radians(lat))
        for col in range(TILE_PX):
            lon = (x + (col + 0.5) / TILE_PX) / n * 360 - 180
            v = 0.0
            for clat, clon, r, s in CELLS:
                dlon = (lon - (clon + DRIFT * hours - 19) % 12 - 19) * k  # drifts east, wraps over 19-31 E
                d2 = (dlon * dlon + (lat - clat) ** 2) / (r * r)
                if d2 < 9:
                    v += s * math.exp(-d2)
            pixels.extend(_colour(v))
    return png(TILE_PX, TILE_PX, bytes(pixels))


# Ice like the cmocean "cmap:ice" style: dark blue thin cover through light blue to white.
ICE_RAMP = [(0.1, (40, 60, 120)), (0.3, (60, 110, 170)), (0.5, (110, 170, 210)), (0.75, (180, 220, 240)),
            (0.95, (245, 250, 255))]


def ice_tile(ts: int, z: int, x: int, y: int) -> bytes:
    """Ice spreading from the eastern Gulf of Finland and the Bothnian Bay, a little more
    each day."""
    grow = (ts // 86400) % 14 / 14
    n = 2**z
    pixels = bytearray()
    for row in range(TILE_PX):
        merc = math.pi * (1 - 2 * (y + (row + 0.5) / TILE_PX) / n)
        lat = math.degrees(math.atan(math.sinh(merc)))
        for col in range(TILE_PX):
            lon = (x + (col + 0.5) / TILE_PX) / n * 360 - 180
            v = max((lon - 25.5 + 3 * grow) / 3 if 59.2 < lat < 60.8 else 0, (lat - 63.5 + grow) / 1.5)
            if v < ICE_RAMP[0][0]:
                pixels.extend((0, 0, 0, 0))
                continue
            rgb = ICE_RAMP[0][1]
            for edge, c in ICE_RAMP:
                if v >= edge:
                    rgb = c
            pixels.extend((*rgb, 255))
    return png(TILE_PX, TILE_PX, bytes(pixels))


class _Response:
    def __init__(self, body: bytes):
        self.status, self.content_type, self.body = 200, "image/png", body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self) -> None:
        pass

    async def read(self) -> bytes:
        return self.body


class FakeTileSession:
    """Answers the simulated overlays' sim:// tile addresses with drawn tiles."""

    def get(self, url: str, **kw) -> _Response:
        _, _, kind, ts, z, x, y = url.split("/")
        if kind == "algae":
            from .simalgae import algae_tile as draw
        else:
            draw = ice_tile if kind == "sea-ice" else rain_tile
        return _Response(draw(int(ts), int(z), int(x), int(y)))


def register(overlays: Overlays) -> None:
    # The real radar's settings, with drawn rain cells for its frames.
    radar.register(overlays, url="sim://rain-radar/{ts}/{z}/{x}/{y}", times=every(300, 12, lag=300),
                   attribution=radar.ATTRIBUTION + " (simulated)")


# --- the ice chart (icechart.py), as the Copernicus WMTS would answer -------------------

def sim_ice(lat: float, lon: float, day: int) -> tuple[float, float]:
    """Made-up ice concentration (%) and thickness (cm) for a day: none until late November,
    growing from the Bothnian Bay and the eastern Gulf of Finland to the end of February,
    then melting away by May."""
    from .icechart import season_start

    d = (day - season_start(day)) / 86400  # days since 1 November
    s = max(0.0, math.sin(math.pi * (d - 20) / 180)) if 20 < d < 200 else 0.0
    if s <= 0:
        return 0.0, 0.0
    conc = 0.0
    # The Bothnian Bay and Sea: ice reaching south as the winter goes on.
    edge = 65.6 - 3.2 * s
    if lat > edge:
        conc = min(100.0, (lat - edge) * 140 + 8 * math.sin(lon * 3))
    # The Gulf of Finland from the east, the archipelago's fast ice along the coast.
    if 59.2 < lat < 60.9:
        east = 30.3 - 7.5 * s
        if lon > east:
            conc = max(conc, min(100.0, (lon - east) * 55 + 10 * math.sin(lat * 9)))
        if lat > 60.12 - 0.1 * s - 0.04 * math.sin(lon * 7) and 21.5 < lon < 27.5:
            conc = 100.0 if s > 0.35 else max(conc, 70.0)  # fast ice along the coast
    conc = max(0.0, conc)
    thick = 0.0
    if conc >= 1:
        thick = min(150.0, 4 + 50 * s * conc / 100 + max(0.0, lat - 63) * 18 * s + 6 * math.sin(lon * 2 + lat * 3))
    return conc, thick


def _grey_of(f: float) -> int:
    """The grey cmocean's "gray" gives a fraction f of its range (even in lightness)."""
    from .icechart import _lstar

    lo, hi = 0, 255
    while lo < hi:
        mid = (lo + hi) // 2
        if _lstar(mid) < f:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _q(url: str) -> dict[str, str]:
    from urllib.parse import parse_qsl, urlsplit

    return {k.upper(): v for k, v in parse_qsl(urlsplit(url).query)}


async def fake_icechart_get(url: str) -> bytes:
    from .icechart import CONC_LAYER, TILE
    from .overlays import parse_instant

    q = _q(url)
    req = q.get("REQUEST", "")
    if req == "GetCapabilities":
        import time as _t
        end = _t.strftime("%Y-%m-%dT00:00:00Z", _t.gmtime(_t.time() - 86400))
        return (f'<Capabilities><Contents><Layer><ows:Identifier>{CONC_LAYER}</ows:Identifier>'
                f'<Dimension><ows:Identifier>time</ows:Identifier><Value>2024-11-01T00:00:00Z/{end}/P1D</Value>'
                '</Dimension></Layer></Contents></Capabilities>').encode()
    day = parse_instant(q["TIME"])
    z, col, row = int(q["TILEMATRIX"]), int(q["TILECOL"]), int(q["TILEROW"])
    thick = "THICK" in q["LAYER"]
    top = 200.0 if thick else 100.0
    n = 2 ** z

    def at(i: float, j: float) -> float:
        merc = math.pi * (1 - 2 * (row + j / TILE) / n)
        lat = math.degrees(math.atan(math.sinh(merc)))
        lon = (col + i / TILE) / n * 360 - 180
        if not (53 <= lat <= 66 and 9 <= lon <= 31):
            return math.nan
        return sim_ice(lat, lon, day)[1 if thick else 0]

    if req == "GetFeatureInfo":
        v = at(int(q["I"]) + 0.5, int(q["J"]) + 0.5)
        props = {} if math.isnan(v) else {"value": v}
        return json.dumps({"type": "FeatureCollection", "features": [{"properties": props}]}).encode()
    # Worked out on a quarter of the pixels and enlarged (Pillow), which is plenty here.
    from PIL import Image

    q = 4
    side = TILE // q
    px = bytearray()
    for j in range(side):
        for i in range(side):
            v = at((i + 0.5) * q, (j + 0.5) * q)
            if math.isnan(v):
                px += b"\0\0\0\0"
            else:
                g = _GREYS[round(min(1.0, v / top) * 1000)]
                px += bytes((g, g, g, 255))
    img = Image.frombytes("RGBA", (side, side), bytes(px)).resize((TILE, TILE), Image.Resampling.NEAREST)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


_GREYS = [_grey_of(k / 1000) for k in range(1001)]


# --- SMHI's ice chart (icemap.py), as its download service would answer ---------------

def shapefile_zip(areas: list[list], day: str) -> bytes:
    """A zipped shapefile like SMHI's: areas as [iceact, type, deformation, min, avg, max,
    rings of [lon, lat]] (outer rings clockwise, as shapefiles have them)."""
    import io
    import struct
    import zipfile

    def cw(ring: list) -> list:
        area = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:]))
        return ring if area < 0 else ring[::-1]

    def ccw(ring: list) -> list:
        return cw(ring)[::-1]

    recs = b""
    index = b""
    off = 50
    for i, a in enumerate(areas):
        rings = [cw(r) if k == 0 else ccw(r) for k, r in enumerate(a[6])]
        pts = [p for r in rings for p in r]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        starts, n = [], 0
        for r in rings:
            starts.append(n)
            n += len(r)
        content = struct.pack("<i4d2i", 5, min(xs), min(ys), max(xs), max(ys), len(rings), n)
        content += struct.pack(f"<{len(starts)}i", *starts)
        content += struct.pack(f"<{2 * n}d", *[v for p in pts for v in p])
        recs += struct.pack(">2i", i + 1, len(content) // 2) + content
        index += struct.pack(">2i", off, len(content) // 2)
        off += 4 + len(content) // 2
    head = lambda length: struct.pack(">7i", 9994, 0, 0, 0, 0, 0, length) + struct.pack("<2i4d4d", 1000, 5, 8, 53, 31, 66, 0, 0, 0, 0)
    shp = head(50 + len(recs) // 2) + recs
    shx = head(50 + len(index) // 2) + index
    fields = [("iceact", "C", 80), ("icetck", "N", 5), ("icemax", "N", 5), ("icemin", "N", 5),
              ("type", "N", 5), ("deformatio", "N", 5), ("chartdate", "C", 80)]
    size = 1 + sum(f[2] for f in fields)
    dbf = struct.pack("<B3BIHH20x", 3, 126, 1, 1, len(areas), 32 + 32 * len(fields) + 1, size)
    for name, kind, length in fields:
        dbf += name.encode().ljust(11, b"\0") + kind.encode() + b"\0" * 4 + bytes([length, 0]) + b"\0" * 14
    dbf += b"\r"
    for a in areas:
        values = [f"{a[0]:02d}", a[4], a[5], a[3], a[1], a[2], day]
        dbf += b" " + b"".join(str(v).encode().ljust(f[2])[:f[2]] if f[1] == "C" else str(v).encode().rjust(f[2])
                               for v, f in zip(values, fields))
    dbf += b"\x1a"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        stem = "seaice_" + day.replace("-", "") + "120000"
        z.writestr(stem + ".shp", shp)
        z.writestr(stem + ".shx", shx)
        z.writestr(stem + ".dbf", dbf)
        z.writestr(stem + ".prj", 'GEOGCS["GCS_WGS_1984"]')
    return out.getvalue()


_SAMPLE: list | None = None


def sim_icemap_day(day: int) -> list[list]:
    """A day's areas: the real chart of 25 Feb 2026 (north of 59.6 N), its ice areas grown or
    shrunk with the sim's season (none in early November, the most in late February)."""
    import time as _t
    from pathlib import Path

    global _SAMPLE
    if _SAMPLE is None:
        _SAMPLE = json.loads((Path(__file__).with_name("sim_icemap_sample.json")).read_text())
    t = _t.gmtime(day)
    # Days since 20 November, the peak at day ~97 (late February), gone by late May.
    from .icechart import season_start
    d = (day - season_start(day)) / 86400 - 19
    grow = max(0.0, min(1.0, d / 97)) if d < 97 else max(0.0, 1 - (d - 97) / 85)
    if grow <= 0.02 or t.tm_mon in (6, 7, 8, 9, 10):
        return []
    out = []
    for a in _SAMPLE:
        lat = max(p[1] for p in a[6][0])
        # The north freezes first and melts last.
        if lat < 66 - grow * 6.5:
            continue
        thin = max(0.25, grow)
        out.append([a[0], a[1], a[2], round(a[3] * thin), round(a[4] * thin), round(a[5] * thin), a[6]])
    return out


async def fake_icemap_get(url: str) -> bytes:
    """SMHI's download service: month listings, day listings and the day's zip."""
    import calendar as _cal
    import re
    import time as _t

    now = _t.time()
    m = re.search(r"/year/(\d+)/month/(\d+)(?:/day/(\d+))?(/data/.*)?", url)
    if not m:
        return b""
    year, month = int(m.group(1)), int(m.group(2))
    if m.group(4):
        day = _cal.timegm((year, month, int(m.group(3)), 0, 0, 0, 0, 0, 0))
        return shapefile_zip(sim_icemap_day(day), _t.strftime("%Y-%m-%d", _t.gmtime(day)))
    if m.group(3):
        stem = f"seaice_{year}{month:02d}{int(m.group(3)):02d}120000"
        href = f"https://opendata-download-icemap.smhi.se/api/version/latest/year/{year}/month/{month}/day/{int(m.group(3))}/data/{stem}.zip"
        return json.dumps({"data": [{"key": stem, "link": [{"href": href, "type": "application/zip"}]}]}).encode()
    days = []
    for d in range(1, _cal.monthrange(year, month)[1] + 1):
        if _cal.timegm((year, month, d, 12, 0, 0, 0, 0, 0)) <= now - 3600:
            days.append({"key": str(d)})
    return json.dumps({"day": days}).encode()
