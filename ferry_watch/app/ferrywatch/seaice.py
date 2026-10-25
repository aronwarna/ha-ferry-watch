"""The Baltic sea ice chart as a map overlay (card T3).

FMI's daily ice concentration analysis, served by Copernicus Marine's WMTS, the same
layer ice.py reads for ice per passage. Listed only from November to May. The days come
from the layer's time dimension in GetCapabilities; if that can't be read, the last two
weeks of days are assumed (the analysis is made each day for 00:00 UTC).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import aiohttp

from .config import USER_AGENT
from .ice import CHART_LAYER, CHART_URL
from .overlays import Overlay, Overlays, every, parse_time_dimension

_LOGGER = logging.getLogger(__name__)

ID = "sea-ice"
SEASON_MONTHS = {11, 12, 1, 2, 3, 4, 5}
DAYS = 14
TIMEOUT = 20
# Copernicus Marine's WMTS: the EPSG:3857 matrix set numbers its levels by zoom, and
# "cmap:ice" is the cmocean ice colour map (dark blue open water to white full ice).
STYLE = "cmap:ice"
URL = (CHART_URL + "?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=" + CHART_LAYER
       + "&STYLE=" + STYLE + "&TILEMATRIXSET=EPSG:3857&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}"
       + "&FORMAT=image/png&TIME={time}")
ATTRIBUTION = "Ice chart: FMI via E.U. Copernicus Marine Service"

_fallback = every(86400, DAYS, lag=86400)
_DIMENSION = re.compile(r"<(?:\w+:)?Dimension\b.*?</(?:\w+:)?Dimension>", re.S)


def in_season(ts: float) -> bool:
    return time.gmtime(ts).tm_mon in SEASON_MONTHS


def times_from_capabilities(xml: str, layer: str = CHART_LAYER) -> list[int]:
    """The days in the layer's time dimension. Copernicus lists all its layers in one
    document; the layer's block is found by its identifier."""
    start = xml.find(f">{layer}<")
    if start < 0:
        return []
    end = xml.find("</Layer>", start)
    block = xml[start:end if end > 0 else None]
    for dim in _DIMENSION.findall(block):
        if re.search(r"<(?:\w+:)?Identifier>\s*time\s*<", dim, re.I):
            values = re.findall(r"<(?:\w+:)?Value>([^<]+)</", dim)
            return parse_time_dimension(",".join(values), limit=DAYS)
    return []


async def list_times(session: aiohttp.ClientSession | None, now: float) -> list[float]:
    if session is not None:
        try:
            params = {"SERVICE": "WMTS", "REQUEST": "GetCapabilities", "VERSION": "1.0.0", "layer": CHART_LAYER}
            async with session.get(CHART_URL, params=params, headers={"User-Agent": USER_AGENT},
                                   timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
                resp.raise_for_status()
                found = times_from_capabilities(await resp.text())
            if found:
                return [t for t in found if t <= now]
            _LOGGER.debug("Sea ice: no time dimension in GetCapabilities, assuming daily")
        except Exception as err:  # noqa: BLE001 - the fallback days usually still work
            _LOGGER.debug("Sea ice GetCapabilities failed (%s), assuming daily", err)
    return await _fallback(session, now)


def overlay(**changes: Any) -> Overlay:
    kw: dict[str, Any] = dict(
        id=ID, title="Sea ice", kind="tiles", url=URL, times=list_times,
        max_age=15 * 86400, refresh=3 * 3600, frame_ms=900, opacity=0.6, color="#7dd3fc",
        min_zoom=4, max_zoom=8, bounds=(53.0, 9.0, 66.0, 31.0), valid_for=86400, season=in_season,
        attribution=ATTRIBUTION,
        note="Daily ice concentration, blue for thin cover to white for solid ice. November to May.",
    )
    kw.update(changes)
    return Overlay(**kw)


def register(overlays: Overlays, **changes: Any) -> None:
    overlays.register(overlay(**changes))
