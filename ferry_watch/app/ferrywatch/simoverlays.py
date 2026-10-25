"""Test overlays for the simulator, so the overlay switches, opacity and time bar work offline.

"Test rain" is a tiles overlay: the last hour in 5-minute frames, each tile a small PNG
(written here with zlib and struct) of rain cells drifting east across the Gulf of Finland.
It goes through the real tile cache, with a session that draws the tiles instead of
fetching them. "Test wind" is a points overlay: a 12 x 10 grid with 48 hourly forecast
times of wind speed, gust and direction.
"""

from __future__ import annotations

import math
import struct
import zlib
from typing import Any

from .overlays import Overlay, Overlays, every

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
    """Answers the test overlay's sim:// tile addresses with drawn tiles."""

    def get(self, url: str, **kw) -> _Response:
        _, _, kind, ts, z, x, y = url.split("/")
        return _Response(rain_tile(int(ts), int(z), int(x), int(y)))


async def fake_wind_run(session: Any, now: float) -> dict[str, Any]:
    first = int(now // 3600 + 1) * 3600
    times = [first + i * 3600 for i in range(48)]
    points = [[59.2 + j * 0.16, 21.6 + i * 0.75] for j in range(10) for i in range(12)]
    values = {}
    for t in times:
        h = (t - first) / 3600
        rows = []
        for lat, lon in points:
            ms = 6 + 4 * math.sin(h / 7 + lon / 3) + 2 * math.cos(lat * 3 + h / 5)
            rows.append([round(ms, 1), round(ms * 1.4, 1), round((200 + 40 * math.sin(h / 9 + lon / 4)) % 360)])
        values[str(t)] = rows
    return {"times": times, "fields": ["ms", "gust", "dir"], "points": points, "values": values}


def register(overlays: Overlays) -> None:
    overlays.register(Overlay(
        id="test-rain", title="Rain (test)", kind="tiles", url="sim://test-rain/{ts}/{z}/{x}/{y}",
        times=every(300, 12, lag=300), max_age=6 * 3600, refresh=300, frame_ms=500, opacity=0.75,
        color="#2563eb", min_zoom=5, max_zoom=10, bounds=(58.5, 19.0, 61.5, 31.0),
        attribution="Test rain (simulated)",
        note="Simulated rain cells, the last hour in 5-minute frames, to try the time bar.",
    ))
    overlays.register(Overlay(
        id="test-wind", title="Wind forecast (test)", kind="points", points=fake_wind_run,
        max_age=3 * 3600, frame_ms=700, opacity=0.9, color="#0f766e",
        attribution="Test wind (simulated)",
        note="A simulated 48-hour wind forecast on a grid of points.",
    ))

