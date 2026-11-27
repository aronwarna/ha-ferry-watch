"""Lightning over Finland and the sea around it in the last hour, from FMI's lightning
location network (open data, CC BY 4.0).

One WFS request (fmi::observations::lightning::multipointcoverage) over a box from the
Stockholm archipelago and Estonia to Oulu, at most every 5 minutes, only while a panel has
the layer on. Each strike comes with its time, peak current (kA, negative for the usual
downward strokes) and whether it stayed in the cloud. The panel draws them as crosses that
fade from white to red with age, like Windy.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .forecast import http_getters
from .overlays import Overlay, Overlays
from .stations import parse_coverage
from .weather import WFS

_LOGGER = logging.getLogger(__name__)

ID = "lightning"
QUERY = "fmi::observations::lightning::multipointcoverage"
PARAMS = "peak_current,cloud_indicator"
BBOX = (17.0, 57.0, 32.0, 66.5)  # west, south, east, north (FMI's bbox is lon,lat)
WINDOW = 3600
EVERY = 300
MAX_STRIKES = 5000  # the newest, in a big storm
ATTRIBUTION = "Lightning: Finnish Meteorological Institute (CC BY 4.0)"

Fetch = Callable[[str, list[tuple[str, str]]], Awaitable[str]]


def request_params(now: float) -> list[tuple[str, str]]:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return [("service", "WFS"), ("version", "2.0.0"), ("request", "getFeature"), ("storedquery_id", QUERY),
            ("parameters", PARAMS), ("bbox", ",".join(str(v) for v in BBOX)),
            ("starttime", time.strftime(fmt, time.gmtime(now - WINDOW))),
            ("endtime", time.strftime(fmt, time.gmtime(now)))]


def build_run(text: str, now: float) -> dict[str, Any]:
    fields, rows = parse_coverage(text, PARAMS)
    ika = fields.index("peak_current") if "peak_current" in fields else None
    icl = fields.index("cloud_indicator") if "cloud_indicator" in fields else None
    rows.sort(key=lambda r: r[2])
    rows = rows[-MAX_STRIKES:]
    t = int(now // EVERY * EVERY)
    points, values = [], []
    for lat, lon, ts, v in rows:
        points.append([lat, lon])
        values.append([round((now - ts) / 60, 1), v[ika] if ika is not None else None,
                       v[icl] if icl is not None else None])
    return {"times": [t], "fields": ["ago", "ka", "cloud"], "points": points, "values": {str(t): values}}


def overlay(fetch: Fetch | None = None, **changes: Any) -> Overlay:
    async def points(session: aiohttp.ClientSession | None, now: float) -> dict[str, Any]:
        get = fetch or http_getters(session)[0]
        return build_run(await get(WFS, request_params(now)), now)

    kw: dict[str, Any] = dict(
        id=ID, title="Lightning", kind="points", points=points, max_age=EVERY, refresh=EVERY, static=True,
        color="#facc15", icon="bolt", group="top", opacity=1.0, min_zoom=3, valid_for=WINDOW,
        bounds=(BBOX[1], BBOX[0], BBOX[3], BBOX[2]), attribution=ATTRIBUTION,
        note="Strikes in the last hour from FMI's lightning network, white when new, fading to red. Every 5 minutes.",
    )
    kw.update(changes)
    return Overlay(**kw)


def register(registry: Overlays, fetch: Fetch | None = None, **changes: Any) -> None:
    registry.register(overlay(fetch, **changes))
