"""Other ships: all AIS traffic around the map view, from Digitraffic's REST API.

Only asked for by the panel while its "Other ships" switch is on (every 30 s and when
the map moves). Nothing is stored or subscribed: each answer is cached for 20 s, and
names, types and lengths come from the vessel directory in SQLite.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .ais import HEADERS, REST, display, rest_feature_to_location
from .store import Store

_LOGGER = logging.getLogger(__name__)

MAX_RADIUS_KM = 60
CACHE_SECONDS = 20
FRESH_SECONDS = 15 * 60  # ships silent for longer (moored, gone) are left out
TIMEOUT = 15

Fetch = Callable[[float, float, int, int], Awaitable[Any]]


def parse_locations(data: Any, now: float, exclude: set[int]) -> list[tuple]:
    """(mmsi, lat, lon, sog, cog, heading) for fresh positions of ships not in `exclude`."""
    out = []
    for feature in (data or {}).get("features") or []:
        props = feature.get("properties") or {}
        try:
            mmsi = int(feature.get("mmsi") or props["mmsi"])
        except (KeyError, TypeError, ValueError):
            continue
        loc = rest_feature_to_location(feature)
        if not loc or mmsi in exclude or now - loc["time"] > FRESH_SECONDS:
            continue
        sog = loc["sog"] if isinstance(loc["sog"], (int, float)) and loc["sog"] < 102.3 else None
        cog = loc["cog"] if isinstance(loc["cog"], (int, float)) and loc["cog"] < 360 else None
        heading = loc["heading"] if isinstance(loc["heading"], (int, float)) and loc["heading"] < 360 else None
        out.append((mmsi, round(loc["lat"], 5), round(loc["lon"], 5), sog, cog, heading))
    return out


class Traffic:
    def __init__(self, session: aiohttp.ClientSession | None, store: Store,
                 exclude: Callable[[], set[int]], fetch: Fetch | None = None):
        self.session = session
        self.store = store
        self.exclude = exclude  # ships already on the map
        self.fetch = fetch or self._get
        self.error: str | None = None
        self._cache: dict[tuple, tuple[float, list[list[Any]]]] = {}
        self._lock = asyncio.Lock()

    async def _get(self, lat: float, lon: float, radius_km: int, from_ms: int) -> Any:
        params = {"latitude": lat, "longitude": lon, "radius": radius_km, "from": from_ms}
        async with self.session.get(f"{REST}/locations", params=params, headers=HEADERS,
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def around(self, lat: float, lon: float, radius_km: float) -> list[list[Any]]:
        """[mmsi, lat, lon, sog, cog, heading, name, type, length, destination] rows."""
        # Rounded, so nearby views share an answer.
        lat, lon = round(lat, 1), round(round(lon / 0.2) * 0.2, 1)
        radius = int(min(MAX_RADIUS_KM, max(10, math.ceil(radius_km / 10) * 10)))
        key = (lat, lon, radius)
        async with self._lock:
            now = time.time()
            hit = self._cache.get(key)
            if hit and now - hit[0] < CACHE_SECONDS:
                return hit[1]
            try:
                data = await self.fetch(lat, lon, radius, int((now - FRESH_SECONDS) * 1000))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                if self.error is None:
                    _LOGGER.warning("Could not read other ships from Digitraffic: %s", err)
                self.error = str(err) or type(err).__name__
                return hit[1] if hit else []
            self.error = None
            ships = parse_locations(data, now, self.exclude())
            del data
            info = self.store.vessel_info(s[0] for s in ships)
            rows = []
            for s in ships:
                v = info.get(s[0], {})
                rows.append([*s, display(v.get("name")), v.get("type"), v.get("length"), v.get("destination")])
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < CACHE_SECONDS}
            self._cache[key] = (now, rows)
            return rows
