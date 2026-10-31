"""The rain radar as a map overlay (card T4).

FMI's Finnish radar composite from its open WMS (openwms.fmi.fi, GeoServer), the last hour
in 5-minute frames, played on the time bar at two frames a second. FMI replaced its old
radar layers (suomi_dbz_eureffin and others, retired November 2026) with
radar_finland_cappi_*; this uses the reflectivity one, radar_finland_cappi_dbzh, which
shows snow as well as rain.

The frames come from the layer's time dimension in GetCapabilities (asked of the layer's
own small capabilities document first, the Radar namespace's if that fails); if neither
can be read, the last hour on the 5-minute clock is assumed. The same document lists the
layer's styles: the winter colour scale (from -20 dBZ, light snow shows) from October to
April, the summer one (from 8 dBZ) otherwise, each only if the server lists it.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import aiohttp

from .config import USER_AGENT
from .overlays import Overlay, Overlays, every, parse_time_dimension

_LOGGER = logging.getLogger(__name__)

ID = "rain-radar"
FRAMES = 12  # the last hour
STEP = 300  # s between frames
LAG = 600  # s a frame takes to appear, when the times have to be assumed
TIMEOUT = 20
BASE = "https://openwms.fmi.fi/geoserver"
NAMESPACE = "Radar"
LAYER = "radar_finland_cappi_dbzh"
CAPS_URLS = (f"{BASE}/{NAMESPACE}/{LAYER}/wms", f"{BASE}/{NAMESPACE}/wms")  # GeoServer layer, namespace
STYLE_WINTER = "radar_dbz_winter_-20_40"
STYLE_SUMMER = "radar_dbz_summer_8_50"
WINTER_MONTHS = {10, 11, 12, 1, 2, 3, 4}
ATTRIBUTION = "Rain radar: Finnish Meteorological Institute (CC BY 4.0)"
# FMI's radars cover Finland and the sea around it (the box of FMI's own example request).
BOUNDS = (57.0, 16.2, 71.8, 35.9)

_fallback = every(STEP, FRAMES, lag=LAG)
_NAME = r"<Name>\s*(?:{ns}:)?{layer}\s*</Name>"
_TIME = re.compile(r"<Dimension\b[^>]*\bname=\"time\"[^>]*>([^<]*)</Dimension>", re.I)
_STYLE = re.compile(r"<Style>\s*<Name>\s*([^<\s]+)\s*</Name>")


def tile_url(style: str) -> str:
    """A WMS 1.3.0 GetMap of one 256-pixel web mercator tile ({bbox} and {time} filled per tile)."""
    return (f"{BASE}/{NAMESPACE}/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={LAYER}"
            f"&STYLES={style}&CRS=EPSG:3857&BBOX={{bbox}}&WIDTH=256&HEIGHT=256"
            "&FORMAT=image/png&TRANSPARENT=TRUE&TIME={time}")


def wanted_style(now: float) -> str:
    return STYLE_WINTER if time.gmtime(now).tm_mon in WINTER_MONTHS else STYLE_SUMMER


def pick_style(styles: list[str], now: float) -> str:
    """The season's colour scale if the server lists it, else the other one, else the
    layer's default (empty)."""
    for style in (wanted_style(now), STYLE_WINTER, STYLE_SUMMER):
        if not styles or style in styles:
            return style
    return ""


def read_capabilities(xml: str, layer: str = LAYER) -> tuple[list[int], list[str]]:
    """The layer's newest FRAMES times and its style names, from a WMS 1.3.0 capabilities
    document (the layer's own, or one listing many layers)."""
    m = re.search(_NAME.format(ns=NAMESPACE, layer=re.escape(layer)), xml)
    if not m:
        return [], []
    end = len(xml)
    for stop in ("<Layer", "</Layer>"):  # the layer's block ends at the next layer or its end
        i = xml.find(stop, m.end())
        if 0 <= i < end:
            end = i
    block = xml[m.end():end]
    dim = _TIME.search(block)
    times = parse_time_dimension(dim.group(1), limit=FRAMES) if dim and dim.group(1).strip() else []
    return times, _STYLE.findall(block)


async def _capabilities(session: aiohttp.ClientSession) -> tuple[list[int], list[str]]:
    params = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetCapabilities"}
    err: Exception | None = None
    for url in CAPS_URLS:
        try:
            async with session.get(url, params=params, headers={"User-Agent": USER_AGENT},
                                   timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
                resp.raise_for_status()
                found = read_capabilities(await resp.text())
            if found[0]:
                return found
        except Exception as e:  # noqa: BLE001 - the next address, or the assumed times
            err = e
    _LOGGER.debug("Rain radar: no times in GetCapabilities (%s), assuming 5-minute frames", err)
    return [], []


def overlay(**changes: Any) -> Overlay:
    kw: dict[str, Any] = dict(
        id=ID, title="Rain radar", kind="tiles", url=tile_url(STYLE_WINTER), times=_fallback,
        max_age=6 * 3600, refresh=STEP, frame_ms=500, opacity=0.7, color="#2563eb",
        min_zoom=4, max_zoom=8, bounds=BOUNDS, valid_for=2 * STEP, attribution=ATTRIBUTION,
        note="FMI's radar for the last hour in 5-minute frames. Press play on the time bar.",
    )
    kw.update(changes)
    o = Overlay(**kw)
    if "times" not in changes:
        async def list_times(session: aiohttp.ClientSession | None, now: float) -> list[float]:
            if session is not None:
                times, styles = await _capabilities(session)
                if times:
                    o.url = tile_url(pick_style(styles, now))  # the next frames fetched use it
                    return [t for t in times if t <= now]
            o.url = tile_url(wanted_style(now))
            return await _fallback(session, now)
        o.times = list_times
    return o


def register(overlays: Overlays, **changes: Any) -> None:
    overlays.register(overlay(**changes))
