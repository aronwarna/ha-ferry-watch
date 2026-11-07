"""Sunset and sunrise: how good each of the next ones is likely to be, everywhere on the
weather model's grid, from where the sun goes down (or comes up) and the clouds.

Own calculation, no extra source: for each sunset and sunrise in the next two days, at
each grid point, the sun's direction and time there, and the weather model's clouds at
that hour, both overhead and along the line towards the sun:

- high and middle cloud overhead catch the colours (best at about half cover);
- low cloud 40-150 km towards the sun blocks the light before it reaches them;
- low cloud overhead and rain hide it all;
- a clear sky still makes a clean sunset, just not a fiery one.

The score is 0-100. The panel then looks, when zoomed in, for places on land with an open
view in the sun's direction (from the terrain), which is where the score is worth going to.
The times on the time bar are the events at Helsinki, on the hour; each point carries its
own event time and direction.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from .overlays import Overlay, Overlays

_LOGGER = logging.getLogger(__name__)

ID = "sunset"
FORECAST = "forecast"
REF = (60.17, 24.94)  # Helsinki: the time bar's events
TOWARD_KM = (40, 80, 120, 160)  # where low cloud towards the sun is looked at
ATTRIBUTION = "Sunset quality: own calculation from the weather model"


# --- the sun ------------------------------------------------------------------------------

def _sun(ts: float) -> tuple[float, float, float]:
    """(right ascension, declination, Greenwich sidereal time in degrees) at a time."""
    d = ts / 86400 - 10957.5
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = math.radians(q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.00000036 * d)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24 * 15
    return ra, dec, gmst


def sun_position(lat: float, lon: float, ts: float) -> tuple[float, float]:
    """The sun's altitude and azimuth (degrees, azimuth from north clockwise)."""
    ra, dec, gmst = _sun(ts)
    ha = math.radians(gmst + lon) - ra
    la = math.radians(lat)
    alt = math.asin(math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha))
    az = math.atan2(-math.sin(ha), math.tan(dec) * math.cos(la) - math.sin(la) * math.cos(ha))
    return math.degrees(alt), math.degrees(az) % 360


HORIZON = -0.833  # degrees: the sun's upper edge on the horizon, with refraction


def events(lat: float, lon: float, start: float, end: float, step: float = 600) -> list[tuple[float, str, float]]:
    """Sunsets and sunrises between two times: [(time, "sunset" | "sunrise", azimuth)]."""
    out = []
    t = start
    prev = sun_position(lat, lon, t)[0] - HORIZON
    while t < end:
        t2 = t + step
        cur = sun_position(lat, lon, t2)[0] - HORIZON
        if (prev > 0) != (cur > 0):
            a, b, fa = t, t2, prev
            for _ in range(20):  # bisect to a few seconds
                m = (a + b) / 2
                fm = sun_position(lat, lon, m)[0] - HORIZON
                if (fa > 0) == (fm > 0):
                    a, fa = m, fm
                else:
                    b = m
            when = (a + b) / 2
            out.append((when, "sunset" if prev > 0 else "sunrise", sun_position(lat, lon, when)[1]))
        t, prev = t2, cur
    return out


def destination(lat: float, lon: float, bearing: float, km: float) -> tuple[float, float]:
    r = km / 6371.0
    la, lo, b = math.radians(lat), math.radians(lon), math.radians(bearing)
    la2 = math.asin(math.sin(la) * math.cos(r) + math.cos(la) * math.sin(r) * math.cos(b))
    lo2 = lo + math.atan2(math.sin(b) * math.sin(r) * math.cos(la), math.cos(r) - math.sin(la) * math.sin(la2))
    return math.degrees(la2), math.degrees(lo2)


# --- the score ----------------------------------------------------------------------------

def score(high: float | None, mid: float | None, low: float | None, toward_low: float | None,
          rain: float | None) -> float:
    """0-100: how colourful the sky is likely to be."""
    high, mid, low = high or 0.0, mid or 0.0, low or 0.0
    canvas = max(high, mid * 0.9) / 100
    colour = 1 - abs(canvas - 0.5) / 0.5 * 0.6  # 1 at half cover, 0.4 clear or overcast
    block = (toward_low if toward_low is not None else low) / 100
    light = 1 - 0.9 * block ** 1.3
    overhead = 1 - 0.7 * (low / 100) ** 2
    wet = 0.4 if (rain or 0) >= 0.3 else 1.0
    return round(max(0.0, min(100.0, 100 * colour * light * overhead * wet)), 1)


class Grid:
    """Nearest-point lookups in a run's regular grid."""

    def __init__(self, run: dict[str, Any]):
        self.lats = sorted({p[0] for p in run["points"]})
        self.lons = sorted({p[1] for p in run["points"]})
        self.index = {(p[0], p[1]): i for i, p in enumerate(run["points"])}
        self.fields = {f: i for i, f in enumerate(run["fields"])}

    def at(self, values: list, lat: float, lon: float, field: str) -> float | None:
        if not (self.lats[0] <= lat <= self.lats[-1] and self.lons[0] <= lon <= self.lons[-1]):
            return None
        la = min(self.lats, key=lambda v: abs(v - lat))
        lo = min(self.lons, key=lambda v: abs(v - lon))
        i = self.index.get((la, lo))
        return values[i][self.fields[field]] if i is not None and field in self.fields else None


def plan(points: list, first: int, last: int, now: float) -> list[tuple[int, str, list]]:
    """The events to score: [(time bar key, kind, [(time, azimuth) or None per point])]."""
    out = []
    for ref_t, kind, _ in events(REF[0], REF[1], max(now - 1800, first), last):
        mine = []
        for lat, lon in points:
            # The point's own event: within a few hours of Helsinki's.
            own = [e for e in events(lat, lon, ref_t - 3 * 3600, ref_t + 3 * 3600, step=900) if e[1] == kind]
            mine.append(min(((e[0], e[2]) for e in own), key=lambda e: abs(e[0] - ref_t)) if own else None)
        out.append((int(round(ref_t / 3600) * 3600), kind, mine))
    return out


def scores(points: list, grid: Grid, events_: list, hours: dict[int, list | None]) -> dict[str, list]:
    values = {}
    for key, _, mine in events_:
        rows = []
        for (lat, lon), ev in zip(points, mine):
            if ev is None:
                rows.append([None, None, None, None, None])  # midnight sun or polar night
                continue
            t, az = ev
            vals = hours.get(int(round(t / 3600) * 3600))
            if vals is None:
                rows.append([None, round(az), int(t), None, None])
                continue
            toward = [grid.at(vals, *destination(lat, lon, az, km), "low") for km in TOWARD_KM]
            toward = [v for v in toward if v is not None]
            tl = sum(toward) / len(toward) if toward else None
            s = score(grid.at(vals, lat, lon, "high"), grid.at(vals, lat, lon, "mid"), grid.at(vals, lat, lon, "low"),
                      tl, grid.at(vals, lat, lon, "rain"))
            rows.append([s, round(az), int(t), grid.at(vals, lat, lon, "cloud"), None if tl is None else round(tl)])
        values[str(key)] = rows
    return values


async def build_run(registry: Overlays, now: float) -> dict[str, Any]:
    run = await registry.run_for(FORECAST, now)
    if not run or not run.get("times"):
        raise ValueError("no weather model yet")
    first, last = run["times"][0], run["times"][-1]
    points = run["points"]
    evs = await asyncio.to_thread(plan, points, first, last, now)
    if not evs:
        raise ValueError("no sunset or sunrise in the forecast")
    wanted = {int(round(ev[0] / 3600) * 3600) for _, _, mine in evs for ev in mine if ev}
    hours = {h: await registry.values_at(FORECAST, h) for h in sorted(wanted) if first <= h <= last}
    values = await asyncio.to_thread(scores, points, Grid(run), evs, hours)
    return {"times": [k for k, _, _ in evs], "fields": ["score", "az", "at", "cloud", "toward"], "points": points,
            "values": values, "info": {"kinds": {str(k): kind for k, kind, _ in evs}}}


def overlay(registry: Overlays, **changes: Any) -> Overlay:
    async def points(session: Any, now: float) -> dict[str, Any]:
        return await build_run(registry, now)

    kw: dict[str, Any] = dict(
        id=ID, title="Sunset & sunrise", kind="points", points=points, max_age=3 * 3600, refresh=1800,
        color="#f97316", icon="sunset", group="field", opacity=0.8, min_zoom=3, frame_ms=1500,
        bounds=registry.registry[FORECAST].bounds if FORECAST in registry.registry else None, valid_for=12 * 3600,
        attribution=ATTRIBUTION,
        note="How colourful each sunset and sunrise of the next two days is likely to be, from the clouds overhead "
             "and towards the sun. Zoom in for spots on land with an open view that way; tap one for why.",
    )
    kw.update(changes)
    return Overlay(**kw)


def register(registry: Overlays, **changes: Any) -> None:
    registry.register(overlay(registry, **changes))
