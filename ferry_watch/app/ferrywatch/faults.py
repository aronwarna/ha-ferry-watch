"""Faulty buoys, beacons and lights from Digitraffic's aids-to-navigation faults
(Fintraffic / Väylävirasto, CC BY 4.0): "Unlit since 3 Oct, Porkkala fairway".

Fetched only when the panel shows the chart, at most every 30 minutes; a small list
in memory. A failure keeps the previous list and is logged once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

import aiohttp

from .ais import HEADERS

_LOGGER = logging.getLogger(__name__)

URL = "https://meri.digitraffic.fi/api/aton/v1/faults"
AREA = (22.7, 59.45, 28.2, 60.75)  # lon/lat box: the Gulf of Finland, as on the drawn chart
CACHE_SECONDS = 30 * 60
RETRY = 5 * 60
TIMEOUT = 20

Fetch = Callable[[], Awaitable[Any]]


def _ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def parse_faults(data: Any, area: tuple[float, float, float, float] = AREA) -> list[dict[str, Any]]:
    """Open faults inside the area, slimmed to what the chart popup shows."""
    out = []
    for feature in (data or {}).get("features") or []:
        props = feature.get("properties") or {}
        try:
            lon, lat = (float(c) for c in feature["geometry"]["coordinates"][:2])
        except (KeyError, TypeError, ValueError):
            continue
        if not (area[0] <= lon <= area[2] and area[1] <= lat <= area[3]):
            continue
        if props.get("fixed") or props.get("fixed_timestamp"):
            continue
        fi, sv = props.get("aton_name_fi"), props.get("aton_name_sv")
        out.append({
            "id": props.get("id"),
            "lat": round(lat, 6), "lon": round(lon, 6),
            "type": props.get("type") or "Fault",
            "since": _ts(props.get("entry_timestamp")),
            "name": fi if not sv or sv == fi else (f"{fi} / {sv}" if fi else sv),
            "aton_type": props.get("aton_type"),
            "fairway": props.get("fairway_name_fi") or props.get("fairway_name_sv"),
        })
    return out


class Faults:
    def __init__(self, session: aiohttp.ClientSession | None, fetch: Fetch | None = None):
        self.session = session
        self.fetch = fetch or self._get
        self.faults: list[dict[str, Any]] = []
        self.fetched = 0.0
        self.error: str | None = None
        self._lock = asyncio.Lock()

    async def _get(self) -> Any:
        async with self.session.get(URL, params={"language": "en"}, headers=HEADERS,
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def get(self) -> list[dict[str, Any]]:
        async with self._lock:
            if time.time() - self.fetched < CACHE_SECONDS:
                return self.faults
            try:
                self.faults = parse_faults(await self.fetch())
                self.fetched = time.time()
                self.error = None
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, AttributeError) as err:
                if self.error is None:
                    _LOGGER.warning("Could not read navigation aid faults: %s", err)
                self.error = str(err) or type(err).__name__
                self.fetched = time.time() - CACHE_SECONDS + RETRY
            return self.faults
