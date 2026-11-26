"""Northern lights: where the aurora is now and the chance of seeing it, with the Kp
forecast for the next three nights.

NOAA's Space Weather Prediction Center (public domain, no key):
- OVATION aurora nowcast (ovation_aurora_latest.json): the chance of aurora overhead on a
  1-degree grid of the whole world for the next half hour or so; only the box over the
  Nordic countries is kept (about 450 points), read with a pattern rather than a JSON parse
  so the 1 MB answer never becomes a million Python objects.
- The planetary K index forecast, 3-hourly for three days.

The chance of actually seeing it at a place also needs a dark, clear sky: the sun at least
12 degrees below the horizon, and the weather model's cloud cover for that hour when the
model has been read (otherwise only darkness counts). Read at most every 15 minutes while
the layer is on.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import aiohttp

from .forecast import http_getters
from .overlays import Overlay, Overlays

_LOGGER = logging.getLogger(__name__)

ID = "aurora"
OVATION = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
KP = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
BOX = (55, 5, 72, 35)  # south, west, north, east (whole degrees)
EVERY = 900
ATTRIBUTION = "Aurora: NOAA Space Weather Prediction Center"
_TRIPLE = re.compile(r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]")
_TIME = re.compile(r'"Forecast Time"\s*:\s*"([^"]+)"')

Get = Callable[[str, Any], Awaitable[str]]


def parse_ovation(text: str) -> tuple[int | None, list[tuple[float, float, float]]]:
    """(forecast time, [(lat, lon, chance %)]) inside BOX."""
    m = _TIME.search(text)
    ts = None
    if m:
        ts = int(datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp())
    s, w, n, e = BOX
    out = []
    for lon, lat, v in _TRIPLE.findall(text):
        lat_f, lon_f = float(lat), float(lon)
        if lon_f > 180:
            lon_f -= 360
        if s <= lat_f <= n and w <= lon_f <= e:
            out.append((lat_f, lon_f, float(v)))
    out.sort()
    return ts, out


def parse_kp(text: str, now: float) -> list[list[float]]:
    """[[time, kp], ...] from three hours ago on (observed, estimated and predicted alike)."""
    data = json.loads(text)
    rows = []
    for row in data:
        if isinstance(row, dict):
            tag, kp = row.get("time_tag"), row.get("kp")
        elif isinstance(row, list) and len(row) >= 2 and row[0] != "time_tag":
            tag, kp = row[0], row[1]
        else:
            continue
        try:
            when = datetime.fromisoformat(str(tag).replace(" ", "T").replace("Z", ""))
            t = int(when.replace(tzinfo=timezone.utc).timestamp())
            k = float(kp)
        except (TypeError, ValueError):
            continue
        if t >= now - 3 * 3600:
            rows.append([t, round(k, 2)])
    return rows


def sun_altitude(lat: float, lon: float, ts: float) -> float:
    """The sun's height above the horizon in degrees (NOAA's simple formulas, ~0.5 degree)."""
    d = ts / 86400 - 10957.5  # days from J2000
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = math.radians(q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.00000036 * d)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    ha = math.radians(gmst * 15 + lon) - ra
    la = math.radians(lat)
    return math.degrees(math.asin(math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha)))


def darkness(alt: float) -> float:
    """1 in a dark sky (sun 12 degrees down or more), 0 in daylight or light twilight."""
    return max(0.0, min(1.0, (-6 - alt) / 6))


def cloud_lookup(run: dict[str, Any] | None, values: list | None):
    """Cloud cover (%) at a place from the weather model's hour, nearest grid point; None
    when there is no model."""
    if not run or not values or "cloud" not in run["fields"]:
        return lambda lat, lon: None
    j = run["fields"].index("cloud")
    lats = sorted({p[0] for p in run["points"]})
    lons = sorted({p[1] for p in run["points"]})
    index = {(p[0], p[1]): i for i, p in enumerate(run["points"])}

    def near(axis, v):
        return min(axis, key=lambda a: abs(a - v))

    def at(lat: float, lon: float) -> float | None:
        if not (lats[0] - 0.3 <= lat <= lats[-1] + 0.3 and lons[0] - 0.6 <= lon <= lons[-1] + 0.6):
            return None
        i = index.get((near(lats, lat), near(lons, lon)))
        return values[i][j] if i is not None else None

    return at


def build_run(ovation: tuple[int | None, list[tuple[float, float, float]]], kp: list[list[float]], now: float,
              clouds=lambda lat, lon: None) -> dict[str, Any]:
    ts, cells = ovation
    t = int((ts or now) // 300 * 300)
    points, values = [], []
    for lat, lon, v in cells:
        dark = darkness(sun_altitude(lat, lon, t))
        c = clouds(lat, lon)
        seen = v * dark * (1 - c / 100 if c is not None else 1)
        points.append([lat, lon])
        values.append([v, round(seen, 1), c, round(dark, 2)])
    if not points:
        raise ValueError("no aurora grid in the answer")
    return {"times": [t], "fields": ["prob", "seen", "cloud", "dark"], "points": points,
            "values": {str(t): values}, "info": {"kp": kp}}


def overlay(registry: Overlays | None = None, get: Get | None = None, **changes: Any) -> Overlay:
    async def points(session: aiohttp.ClientSession | None, now: float) -> dict[str, Any]:
        text_get = get or http_getters(session)[0]
        ovation = parse_ovation(await text_get(OVATION, {}))
        try:
            kp = parse_kp(await text_get(KP, {}), now)
        except Exception as err:  # noqa: BLE001 - the map still works without
            _LOGGER.debug("Kp forecast failed: %s", err)
            kp = []
        clouds = lambda lat, lon: None  # noqa: E731
        if registry is not None and "forecast" in registry.registry:
            try:
                run = await registry.run_for("forecast", now)
                hour = int(now // 3600 * 3600)
                if run and hour in run["times"]:
                    clouds = cloud_lookup(run, await registry.values_at("forecast", hour))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Aurora: no clouds (%s)", err)
        return build_run(ovation, kp, now, clouds)

    kw: dict[str, Any] = dict(
        id=ID, title="Northern lights", kind="points", points=points, max_age=EVERY, refresh=EVERY, static=True,
        color="#22c55e", icon="aurora", group="field", opacity=0.8, min_zoom=3, valid_for=3 * 3600,
        bounds=(BOX[0], BOX[1], BOX[2], BOX[3]), attribution=ATTRIBUTION,
        note="NOAA's aurora nowcast: the chance of northern lights overhead in the next half hour, and of "
             "seeing them here with the dark and the clouds. The Kp forecast says what the next nights may bring.",
    )
    kw.update(changes)
    return Overlay(**kw)


def register(registry: Overlays, get: Get | None = None, **changes: Any) -> None:
    registry.register(overlay(registry, get, **changes))
