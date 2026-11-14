"""Light pollution: how bright the night sky is made by artificial light, from David
Lorenz's Light Pollution Atlas 2025 (a recalculation of the World Atlas of the Artificial
Night Sky Brightness from NOAA VIIRS satellite data of 2025, by the Earth Observation Group,
Colorado School of Mines).

The atlas comes as gzipped binary tiles of 5 x 5 degrees on a 1/120-degree grid (about
1 km), the ones its own map reads on a tap. The twelve covering 55-70 N, 15-35 E are read
once (about 1 MB) and kept decoded as one grid of 2-byte values on disk (8.6 MB); map tiles
are drawn from it with Pillow when first asked for (to zoom 9, enlarged beyond) and a tap
reads its own few bytes. Nothing of it stays in memory.

Colours follow the atlas's own zones (dark grey to blue, green, yellow, orange, red and
white in city centres), blended smoothly between them; the zones are the brightness at
zenith against a natural sky, not the Bortle scale, as the atlas's author asks.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import logging
import math
import mmap
import os
import struct
import time
from array import array
from itertools import accumulate
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from .config import USER_AGENT
from .overlays import Overlay, Overlays, tile_bounds

_LOGGER = logging.getLogger(__name__)

ID = "light-pollution"
YEAR = 2025
BASE = "https://djlorenz.github.io/astronomy/binary_tiles/{year}/binary_tile_{x}_{y}.dat.gz"
TILES_X = range(40, 44)  # 15-35 E
TILES_Y = range(25, 28)  # 55-70 N
N = 600  # values a side per atlas tile
RES = 120  # per degree
WEST, SOUTH = 5 * (TILES_X[0] - 1) - 180, 5 * (TILES_Y[0] - 1) - 65
COLS, ROWS = N * len(TILES_X), N * len(TILES_Y)
NONE = 0xFFFF
MAX_ZOOM = 9
ATTRIBUTION = "Light pollution: Light Pollution Atlas 2025, D. Lorenz (VIIRS: EOG, Colorado School of Mines)"
TIMEOUT = 60

# The atlas's zones: (upper brightness ratio, name, colour).
ZONES = [(0.01, "0", (0, 0, 0)), (0.06, "1a", (34, 34, 34)), (0.11, "1b", (66, 66, 66)),
         (0.19, "2a", (20, 47, 114)), (0.33, "2b", (33, 84, 216)), (0.58, "3a", (15, 87, 20)),
         (1.00, "3b", (31, 161, 42)), (1.73, "4a", (110, 100, 30)), (3.00, "4b", (184, 166, 37)),
         (5.20, "5a", (191, 100, 30)), (9.00, "5b", (253, 150, 80)), (15.59, "6a", (251, 90, 73)),
         (27.00, "6b", (251, 153, 138)), (46.77, "7a", (160, 160, 160)), (math.inf, "7b", (242, 242, 242))]
ZONE_TEXT = {"0": "pristine dark sky", "1": "excellent dark sky", "2": "dark sky, Milky Way strong",
             "3": "rural sky, Milky Way clear", "4": "rural/suburban, Milky Way faint", "5": "suburban sky",
             "6": "bright suburban sky", "7": "city sky"}

Get = Callable[[str], Awaitable[bytes]]


def ratio(compressed: int) -> float:
    """The atlas's stored value as artificial brightness / natural brightness."""
    return (5.0 / 195.0) * (math.exp(0.0195 * compressed) - 1.0)


def sqm(r: float) -> float:
    """Sky brightness in magnitudes per square arcsecond (natural sky 22.0)."""
    return 22.0 - 5.0 * math.log(1.0 + r) / math.log(100.0)


def zone(r: float) -> str:
    for upper, name, _ in ZONES:
        if r < upper:
            return name
    return ZONES[-1][1]


def decode_tile(raw: bytes) -> array:
    """One atlas tile (gunzipped) as N x N values, south row first, west to east.

    The format, as the atlas's own map reads it: the south-west value in two bytes, then
    signed byte steps: down the first column (each row's byte 1), then along each row."""
    d = array("b", raw)
    first = 128 * d[0] + d[1]
    starts = list(accumulate((d[N * i + 1] for i in range(1, N)), initial=first))
    out = array("H")
    for r in range(N):
        o = N * r + 1
        row = accumulate((d[o + i] for i in range(1, N)), initial=starts[r])
        out.extend(min(max(v, 0), 0xFFFE) for v in row)
    return out


class Atlas:
    """The decoded grid on disk, read through mmap."""

    def __init__(self, directory: Path, session: aiohttp.ClientSession | None, get: Get | None = None):
        self.dir = directory
        self.path = directory / f"grid{YEAR}.bin"
        self.session = session
        self.get = get or self._get
        self._lock = asyncio.Lock()
        self._mm: mmap.mmap | None = None
        self._file = None

    async def _get(self, url: str) -> bytes:
        if self.session is None:
            raise ValueError("no connection")
        async with self.session.get(url, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def ready(self) -> None:
        if self._mm is not None:
            return
        async with self._lock:
            if self._mm is not None:
                return
            if not self.path.exists():
                await self._build()
            self._file = open(self.path, "rb")  # noqa: SIM115 - kept open for the mmap
            self._mm = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)

    async def _build(self) -> None:
        _LOGGER.info("Reading the light pollution atlas (%d tiles)", len(TILES_X) * len(TILES_Y))
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tiles: dict[tuple[int, int], bytes] = {}
        for ty in TILES_Y:
            for tx in TILES_X:
                tiles[(tx, ty)] = await self.get(BASE.format(year=YEAR, x=tx, y=ty))
        await asyncio.to_thread(self._write, tiles, tmp)
        os.replace(tmp, self.path)

    @staticmethod
    def _write(tiles: dict[tuple[int, int], bytes], tmp: Path) -> None:
        with open(tmp, "wb") as f:
            for ty in TILES_Y:
                decoded = [decode_tile(gzip.decompress(tiles[(tx, ty)])) for tx in TILES_X]
                for r in range(N):
                    for a in decoded:
                        f.write(a[N * r: N * r + N].tobytes())
                del decoded

    def raw(self, row: int, col: int) -> int:
        if not (0 <= row < ROWS and 0 <= col < COLS) or self._mm is None:
            return NONE
        return struct.unpack_from("=H", self._mm, 2 * (row * COLS + col))[0]

    def at(self, lat: float, lon: float) -> float | None:
        """The brightness ratio at a place, between the four grid values around it."""
        y = (lat - SOUTH) * RES - 0.5
        x = (lon - WEST) * RES - 0.5
        r0, c0 = math.floor(y), math.floor(x)
        fy, fx = y - r0, x - c0
        total = weight = 0.0
        for dr, wy in ((0, 1 - fy), (1, fy)):
            for dc, wx in ((0, 1 - fx), (1, fx)):
                v = self.raw(r0 + dr, c0 + dc)
                if v != NONE and wy * wx > 0:
                    total += ratio(v) * wy * wx
                    weight += wy * wx
        return total / weight if weight else None

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._file.close()
            self._mm = self._file = None


def _stops() -> list[tuple[float, tuple[int, int, int]]]:
    out, lower = [], 0.002
    for upper, _, rgb in ZONES:
        top = upper if upper != math.inf else 150.0
        out.append((math.log10(math.sqrt(lower * top)), rgb))  # each zone's colour at its middle
        lower = upper
    return out


STOPS = _stops()


def colour(r: float) -> tuple[int, int, int, int]:
    """A smooth colour between the zones' (in the logarithm of the ratio); dark skies more
    see-through so the map shows."""
    lr = math.log10(max(r, 1e-4))
    alpha = round(max(110, min(225, 170 + 55 * lr / 1.5)))
    if lr <= STOPS[0][0]:
        return (*STOPS[0][1], alpha)
    for (e0, c0), (e1, c1) in zip(STOPS, STOPS[1:]):
        if lr <= e1:
            t = (lr - e0) / (e1 - e0)
            return (*(round(c0[i] + (c1[i] - c0[i]) * t) for i in range(3)), alpha)
    return (*STOPS[-1][1], alpha)


_PALETTE: list[tuple[int, int, int, int]] | None = None


def palette() -> list[tuple[int, int, int, int]]:
    """Colours for every stored value up to where it stops changing (lookups, not maths)."""
    global _PALETTE
    if _PALETTE is None:
        _PALETTE = [colour(ratio(v)) for v in range(512)]
    return _PALETTE


def draw_tile(atlas: Atlas, z: int, x: int, y: int, size: int = 256) -> bytes | None:
    from PIL import Image  # lazy: Pillow only when tiles are made

    s, w, n, e = tile_bounds(z, x, y)
    if n < SOUTH or s > SOUTH + ROWS / RES or e < WEST or w > WEST + COLS / RES:
        return None
    pal = palette()
    last = len(pal) - 1
    px = bytearray(size * size * 4)
    world = 2 ** z * size
    cols = [((x * size + c + 0.5) / world * 360 - 180 - WEST) * RES - 0.5 for c in range(size)]
    any_value = False
    for r in range(size):
        merc = math.pi * (1 - 2 * (y * size + r + 0.5) / world)
        lat = math.degrees(math.atan(math.sinh(merc)))
        gy = (lat - SOUTH) * RES - 0.5
        r0 = math.floor(gy)
        fy = gy - r0
        o = r * size * 4
        for c, gx in enumerate(cols):
            c0 = math.floor(gx)
            fx = gx - c0
            a, b = atlas.raw(r0, c0), atlas.raw(r0, c0 + 1)
            d, f = atlas.raw(r0 + 1, c0), atlas.raw(r0 + 1, c0 + 1)
            if NONE in (a, b, d, f):
                continue
            v = (a * (1 - fx) + b * fx) * (1 - fy) + (d * (1 - fx) + f * fx) * fy
            px[o + 4 * c: o + 4 * c + 4] = bytes(pal[min(last, int(v + 0.5))])
            any_value = True
    if not any_value:
        return None
    img = Image.frombytes("RGBA", (size, size), bytes(px))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=False, compress_level=6)
    return out.getvalue()


def overlay(directory: Path, session: aiohttp.ClientSession | None, get: Get | None = None, **changes: Any) -> Overlay:
    atlas = Atlas(directory, session, get)
    stamp = int(time.mktime((YEAR, 7, 1, 0, 0, 0, 0, 0, 0)))

    async def times(session_: Any, now: float) -> list[float]:
        return [stamp]

    async def tile(ts: int, z: int, x: int, y: int) -> bytes | None:
        await atlas.ready()
        return await asyncio.to_thread(draw_tile, atlas, z, x, y)

    async def value(lat: float, lon: float, ts: int) -> dict[str, Any] | None:
        await atlas.ready()
        r = atlas.at(lat, lon)
        if r is None:
            return None
        zname = zone(r)
        return {"ratio": round(r, 3), "sqm": round(sqm(r), 2), "zone": zname, "text": ZONE_TEXT[zname[0]],
                "year": YEAR}

    kw: dict[str, Any] = dict(
        id=ID, title="Light pollution", kind="local", times=times, tile=tile, value=value, static=True,
        max_age=365 * 86400, refresh=86400, color="#a3a3a3", icon="lightpollution", group="field", opacity=0.75,
        min_zoom=3, max_zoom=MAX_ZOOM, bounds=(SOUTH, WEST, SOUTH + ROWS / RES, WEST + COLS / RES),
        attribution=ATTRIBUTION,
        note="How much artificial light brightens the night sky (Light Pollution Atlas 2025). "
             "Tap the map for the zone and the sky brightness in mag/arcsec².",
    )
    kw.update(changes)
    return Overlay(**kw)


def register(registry: Overlays, directory: Path, session: aiohttp.ClientSession | None, get: Get | None = None,
             **changes: Any) -> None:
    registry.register(overlay(directory, session, get, **changes))
