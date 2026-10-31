"""Test overlays for the simulator, so the overlay switches, opacity and time bar work offline.

The rain radar runs with its real settings, its frames small PNGs (written here with zlib
and struct) of rain cells drifting east across the Gulf of Finland.
It goes through the real tile cache, with a session that draws the tiles instead of
fetching them. The wind forecast (a points overlay) is registered with the stations, its
FMI answers made up in simsea.py.
"""

from __future__ import annotations

import math
import struct
import zlib
from typing import Any

from . import radar, seaice
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
        draw = ice_tile if kind == "sea-ice" else rain_tile
        return _Response(draw(int(ts), int(z), int(x), int(y)))


def register(overlays: Overlays) -> None:
    # The real ice chart's settings, drawn here and shown all year so it can be tried.
    seaice.register(overlays, url="sim://sea-ice/{ts}/{z}/{x}/{y}", times=every(86400, 14, lag=86400),
                    season=None, attribution=seaice.ATTRIBUTION + " (simulated)")
    # The real radar's settings, with drawn rain cells for its frames.
    radar.register(overlays, url="sim://rain-radar/{ts}/{z}/{x}/{y}", times=every(300, 12, lag=300),
                   attribution=radar.ATTRIBUTION + " (simulated)")
