"""Other ships: all AIS traffic around the map view, from Digitraffic's REST API.

Only asked for by the panel while its "Other ships" switch is on (every 30 s and when
the map moves). Nothing is stored or subscribed: each answer is cached for 20 s, and
names, types and lengths come from the vessel directory in SQLite.

Views up to 60 km across ask Digitraffic for a circle. Wider views (zoomed out to the
whole Gulf of Finland or the Baltic) share one snapshot of every fresh position Digitraffic
has (usually 1,000-3,000 ships), read item by item and kept for 30 s as compact rows
(about 150 bytes a ship), and only the ships inside the view go to the panel.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .ais import HEADERS, REST, display, iter_json_array, rest_feature_to_location
from .store import Store

_LOGGER = logging.getLogger(__name__)

MAX_RADIUS_KM = 60
CACHE_SECONDS = 20
ALL_CACHE_SECONDS = 30
MAX_ROWS = 4000  # the most ships sent to the panel at once (moving ones first)
FRESH_SECONDS = 15 * 60  # ships silent for longer (moored, gone) are left out
TIMEOUT = 15

# (lat, lon, radius km, from ms) -> GeoJSON; lat None asks for every ship. May return text.
Fetch = Callable[[float | None, float | None, int, int], Awaitable[Any]]


def iter_features(data: Any) -> Any:
    """Features of a parsed FeatureCollection, or of its JSON text one at a time."""
    if isinstance(data, str):
        i = data.find('"features"')
        return iter_json_array(data, i) if i >= 0 else iter(())
    return (data or {}).get("features") or []


def parse_locations(data: Any, now: float, exclude: set[int]) -> list[tuple]:
    """(mmsi, lat, lon, sog, cog, heading) for fresh positions of ships not in `exclude`."""
    out = []
    for feature in iter_features(data):
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
        self._all: tuple[float, list[list[Any]]] | None = None  # (when, rows) for wide views
        self._lock = asyncio.Lock()

    async def _get(self, lat: float | None, lon: float | None, radius_km: int, from_ms: int) -> Any:
        params: dict[str, Any] = {"from": from_ms}
        if lat is not None:
            params.update(latitude=lat, longitude=lon, radius=radius_km)
        async with self.session.get(f"{REST}/locations", params=params, headers=HEADERS,
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT * (1 if lat is not None else 2))) as resp:
            resp.raise_for_status()
            if lat is None:
                return await resp.text()  # parsed a ship at a time (see iter_features)
            return await resp.json(content_type=None)

    def _rows(self, data: Any, now: float) -> list[list[Any]]:
        return self._join(parse_locations(data, now, self.exclude()))

    def _join(self, ships: list[tuple]) -> list[list[Any]]:
        info = self.store.vessel_info(s[0] for s in ships)
        rows = []
        for s in ships:
            v = info.get(s[0], {})
            rows.append([*s, display(v.get("name")), v.get("type"), v.get("length"), v.get("destination")])
        return rows

    async def in_view(self, south: float, west: float, north: float, east: float) -> list[list[Any]]:
        """Rows (as around()) for every ship inside the box, from the shared snapshot."""
        async with self._lock:
            now = time.time()
            if not self._all or now - self._all[0] >= ALL_CACHE_SECONDS:
                try:
                    data = await self.fetch(None, None, 0, int((now - FRESH_SECONDS) * 1000))
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                    if self.error is None:
                        _LOGGER.warning("Could not read other ships from Digitraffic: %s", err)
                    self.error = str(err) or type(err).__name__
                    data = None
                if data is not None:
                    self.error = None
                    # A few thousand ships take a moment to read: off the event loop. SQLite stays on it.
                    ships = await asyncio.to_thread(parse_locations, data, now, self.exclude())
                    rows = self._join(ships)
                    del data
                    self._all = (now, rows)
            rows = self._all[1] if self._all else []
        inside = [r for r in rows if south <= r[1] <= north and west <= r[2] <= east]
        if len(inside) > MAX_ROWS:
            inside.sort(key=lambda r: -(r[3] or 0))  # moving ships first
            del inside[MAX_ROWS:]
        return inside

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
            rows = self._rows(data, now)
            del data
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < CACHE_SECONDS}
            self._cache[key] = (now, rows)
            return rows
