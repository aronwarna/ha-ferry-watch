"""Renders a small map picture of a ship and its recent track for notifications."""

from __future__ import annotations

import asyncio
import io
import logging
import math
from typing import Awaitable, Callable

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from .config import USER_AGENT

_LOGGER = logging.getLogger(__name__)

TILE = 256
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}"
ATTRIBUTION = "Map © Esri, HERE, Garmin, © OpenStreetMap contributors"

TileFetcher = Callable[[int, int, int], Awaitable[bytes | None]]


def _world_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    scale = TILE * 2**z
    x = (lon + 180) / 360 * scale
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def choose_zoom(points: list[tuple[float, float]], width: int, height: int, pad: int,
                min_zoom: int = 6, max_zoom: int = 13) -> int:
    for z in range(max_zoom, min_zoom - 1, -1):
        xs, ys = zip(*(_world_px(lat, lon, z) for lat, lon in points))
        if max(xs) - min(xs) <= width - 2 * pad and max(ys) - min(ys) <= height - 2 * pad:
            return z
    return min_zoom


async def render(points: list[tuple[float, float]], ship: tuple[float, float, float | None],
                 color: str, fetch_tile: TileFetcher, width: int = 800, height: int = 400,
                 extra: list[tuple[float, float]] | None = None) -> bytes:
    """PNG of the track (oldest first) ending at the ship. `extra` points (e.g. the phone)
    are kept in view and drawn as a small dot."""
    view = list(points) + [ship[:2]] + list(extra or [])
    z = choose_zoom(view, width, height, pad=50)
    xs, ys = zip(*(_world_px(lat, lon, z) for lat, lon in view))
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    left, top = cx - width / 2, cy - height / 2

    canvas = Image.new("RGB", (width, height), (170, 211, 223))
    tx0, ty0 = int(left // TILE), int(top // TILE)
    tx1, ty1 = int((left + width) // TILE), int((top + height) // TILE)
    coords = [(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)]
    tiles = await asyncio.gather(*(fetch_tile(z, tx, ty) for tx, ty in coords))
    for (tx, ty), data in zip(coords, tiles):
        if not data:
            continue
        try:
            tile = Image.open(io.BytesIO(data)).convert("RGB")
        except OSError:
            continue
        canvas.paste(tile, (int(tx * TILE - left), int(ty * TILE - top)))

    draw = ImageDraw.Draw(canvas, "RGBA")
    rgb = _hex(color)

    def px(lat: float, lon: float) -> tuple[float, float]:
        x, y = _world_px(lat, lon, z)
        return x - left, y - top

    line = [px(lat, lon) for lat, lon in points] + [px(*ship[:2])]
    if len(line) > 1:
        draw.line(line, fill=(255, 255, 255, 230), width=9, joint="curve")
        draw.line(line, fill=rgb + (255,), width=5, joint="curve")

    for lat, lon in extra or []:
        x, y = px(lat, lon)
        draw.ellipse([x - 7, y - 7, x + 7, y + 7], fill=(17, 24, 39, 255), outline=(255, 255, 255, 255), width=3)

    # Ship: an arrow pointing along its heading.
    sx, sy = px(*ship[:2])
    heading = math.radians(ship[2] or 0)
    shape = [(0, -24), (14, 16), (0, 8), (-14, 16)]
    pts = [(sx + x * math.cos(heading) - y * math.sin(heading),
            sy + x * math.sin(heading) + y * math.cos(heading)) for x, y in shape]
    draw.polygon(pts, fill=rgb + (255,), outline=(255, 255, 255, 255), width=3)

    font = ImageFont.load_default(size=12)
    tw = draw.textlength(ATTRIBUTION, font=font)
    draw.rectangle([width - tw - 10, height - 18, width, height], fill=(255, 255, 255, 190))
    draw.text((width - tw - 5, height - 16), ATTRIBUTION, fill=(60, 60, 60, 255), font=font)

    out = io.BytesIO()
    canvas.save(out, "PNG", optimize=True)
    return out.getvalue()


def http_tile_fetcher(session: aiohttp.ClientSession) -> TileFetcher:
    async def fetch(z: int, x: int, y: int) -> bytes | None:
        x %= 2**z
        try:
            async with session.get(TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT},
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
    return fetch
