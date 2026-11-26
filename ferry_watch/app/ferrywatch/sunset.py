"""Sunset and sunrise: how colourful each of the next ones is likely to be, everywhere on the
weather model's grid, from where the sun goes down (or comes up) and the clouds.

Own calculation, no extra source. A colourful sunset needs three things, and the score
(0-100) looks for each in the weather model's clouds, at the very minute of each place's
own sunset or sunrise (between the model's hours) and along the line towards the sun:

1. Something to light up: high and middle cloud over the place and in the part of the sky
   towards the sun, out to about 120 km (that is where the colours are seen). Best at
   about half cover; a clear sky gives a clean sunset with little colour, an overcast
   one can still glow if the light gets in under it.
2. A way in for the light. The reddest light reaches a cloud just before it goes dark
   there, after travelling low over the earth towards the sun: for high cloud (about
   8 km) it passes under 2 km some 190-410 km further towards the sun, for middle cloud
   (about 4 km) some 80-310 km. Low cloud there (and for high cloud, middle cloud from
   about 90 km) shades the canvas grey. The weather model doesn't reach that far
   everywhere; what it can't see counts as a third cloudy, and the panel says so.
3. A view: low cloud over the place hides it all; fog and haze dim it; rain kills it.

Each point also gets the time its own sun sets or rises and when the high clouds stop
glowing (the sun 3.5 degrees down), which is when the colours are best.

The times on the time bar are the events at Helsinki, on the hour; each point carries its
own event time and direction. The panel then looks, when zoomed in, for places on land
with an open view in the sun's direction (from the terrain).
"""

from __future__ import annotations

import asyncio
import logging
import math
from array import array
from bisect import bisect_right
from typing import Any

from .overlays import MISSING, Overlay, Overlays, RunBuilder

_LOGGER = logging.getLogger(__name__)

ID = "sunset"
FORECAST = "forecast"
REF = (60.17, 24.94)  # Helsinki: the time bar's events
ATTRIBUTION = "Sunset quality: own calculation from the weather model"
HORIZON = -0.833  # degrees: the sun's upper edge on the horizon, with refraction
GLOW_DIP = 3.5  # degrees: high cloud stops glowing when the sun is this far down

# Where the clouds are looked at, km towards the sun, with weights.
CANVAS_HIGH = [(0, 0.25), (30, 0.25), (60, 0.2), (90, 0.15), (120, 0.15)]
CANVAS_MID = [(0, 0.35), (25, 0.3), (50, 0.2), (80, 0.15)]
NEAR = [(0, 0.5), (15, 0.3), (35, 0.2)]  # low cloud over the viewer (hides the sky)
HORIZON_LOW = [(20, 0.3), (50, 0.4), (80, 0.3)]  # low cloud hiding the sun itself
PATH = [50, 100, 150, 200, 250, 300, 350, 400, 450]
UNKNOWN = 0.35  # cover assumed where the model doesn't reach


def _zone(centre: float, half: float) -> list[tuple[float, float]]:
    """Weights along PATH, highest at `centre` km and none `half` km either side."""
    return [(km, max(0.0, 1 - abs(km - centre) / half)) for km in PATH if abs(km - centre) < half]


# The light's way under each canvas: (cloud field, where, how much it blocks). Distances
# from the place: the canvas is about 30 km towards the sun from it. Past its lowest point
# the light climbs again, through the middle levels some 350-450 km away: a middle-level
# deck reaching that far keeps it from the middle cloud over the place (overcast, grey).
BLOCK = {
    "high": [("low", _zone(330, 150), 1.0), ("mid", _zone(280, 190), 0.7)],
    "mid": [("low", _zone(225, 140), 1.0), ("mid", _zone(400, 100), 0.8)],
}
FIELDS = ["score", "az", "at", "glow", "canvas", "high", "mid", "gap", "low", "vis", "seen", "rain"]
# Kept whole, but times as minutes from the time bar's hour, visibility and rain in tenths.
SCALE = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 10.0, 1.0, 10.0]
WORDS = [(80, "fiery"), (60, "great"), (40, "good"), (20, "fair"), (0, "dull")]


def word(score: float) -> str:
    return next(w for limit, w in WORDS if score >= limit)


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


def _cross(lat: float, lon: float, a: float, b: float, level: float, fa: float | None = None) -> float:
    """The moment between a and b (where the sun is on either side of `level`) it crosses it."""
    fa = sun_position(lat, lon, a)[0] - level if fa is None else fa
    for _ in range(14):  # to about a second over half an hour
        m = (a + b) / 2
        fm = sun_position(lat, lon, m)[0] - level
        if (fa > 0) == (fm > 0):
            a, fa = m, fm
        else:
            b = m
    return (a + b) / 2


def events(lat: float, lon: float, start: float, end: float, step: float = 600,
           level: float = HORIZON) -> list[tuple[float, str, float]]:
    """Sunsets and sunrises between two times: [(time, "sunset" | "sunrise", azimuth)]."""
    out = []
    t = start
    prev = sun_position(lat, lon, t)[0] - level
    while t < end:
        t2 = t + step
        cur = sun_position(lat, lon, t2)[0] - level
        if (prev > 0) != (cur > 0):
            when = _cross(lat, lon, t, t2, level, prev)
            out.append((when, "sunset" if prev > 0 else "sunrise", sun_position(lat, lon, when)[1]))
        t, prev = t2, cur
    return out


def glow_time(lat: float, lon: float, t: float, kind: str) -> float:
    """When the colours end after a sunset (or start before a sunrise): the sun GLOW_DIP
    down; within 90 minutes (the light summer nights, where it never gets that low)."""
    span = 5400.0
    a, b = (t, t + span) if kind == "sunset" else (t - span, t)
    fa = sun_position(lat, lon, a)[0] + GLOW_DIP
    fb = sun_position(lat, lon, b)[0] + GLOW_DIP
    if (fa > 0) == (fb > 0):
        return b if kind == "sunset" else a
    return _cross(lat, lon, a, b, -GLOW_DIP, fa)


def destination(lat: float, lon: float, bearing: float, km: float) -> tuple[float, float]:
    r = km / 6371.0
    la, lo, b = math.radians(lat), math.radians(lon), math.radians(bearing)
    la2 = math.asin(math.sin(la) * math.cos(r) + math.cos(la) * math.sin(r) * math.cos(b))
    lo2 = lo + math.atan2(math.sin(b) * math.sin(r) * math.cos(la), math.cos(r) - math.sin(la) * math.sin(la2))
    return math.degrees(la2), math.degrees(lo2)


# --- the clouds on the way ----------------------------------------------------------------

class Grid:
    """Values between the points of a run's regular grid (bilinear, leaving out the missing)."""

    def __init__(self, run: dict[str, Any]):
        self.lats = sorted({p[0] for p in run["points"]})
        self.lons = sorted({p[1] for p in run["points"]})
        self.index = {(p[0], p[1]): i for i, p in enumerate(run["points"])}
        self.fields = {f: i for i, f in enumerate(run["fields"])}
        self.nf = len(run["fields"])
        self.scale = run.get("scale") or [10.0] * self.nf

    def value(self, values: array | list | None, i: int, f: int) -> float | None:
        if values is None:
            return None
        if isinstance(values, array):
            v = values[i * self.nf + f]
            return None if v == MISSING else v / self.scale[f]
        return values[i][f]

    @staticmethod
    def _cell(axis: list[float], v: float) -> tuple[int, float] | None:
        if not axis[0] <= v <= axis[-1] or len(axis) < 2:
            return None
        k = min(len(axis) - 2, bisect_right(axis, v) - 1)
        return k, (v - axis[k]) / (axis[k + 1] - axis[k])

    def at(self, values: array | list | None, lat: float, lon: float, field: str) -> float | None:
        f = self.fields.get(field)
        a, b = self._cell(self.lats, lat), self._cell(self.lons, lon)
        if f is None or a is None or b is None or values is None:
            return None
        (j, fy), (k, fx) = a, b
        total = weight = 0.0
        for dj, wy in ((0, 1 - fy), (1, fy)):
            for dk, wx in ((0, 1 - fx), (1, fx)):
                w = wy * wx
                if w <= 0:
                    continue
                i = self.index.get((self.lats[j + dj], self.lons[k + dk]))
                v = None if i is None else self.value(values, i, f)
                if v is not None:
                    total += v * w
                    weight += w
        return total / weight if weight >= 0.3 else None


class Sky:
    """The weather model's fields at the minute of an event: between the hours around it."""

    def __init__(self, grid: Grid, hours: dict[int, Any], t: float):
        self.grid = grid
        h0 = int(t // 3600 * 3600)
        w = (t - h0) / 3600
        self.parts = [(hours.get(h0), 1 - w), (hours.get(h0 + 3600), w)]

    def __call__(self, lat: float, lon: float, field: str) -> float | None:
        total = weight = 0.0
        for values, w in self.parts:
            if w <= 0 or values is None:
                continue
            v = self.grid.at(values, lat, lon, field)
            if v is not None:
                total += v * w
                weight += w
        return total / weight if weight > 0 else None


def along(sky: Sky, lat: float, lon: float, az: float, field: str,
          weights: list[tuple[float, float]]) -> tuple[float | None, float]:
    """A field's weighted mean along the line towards the sun, and the share of the
    weight the model covers."""
    s = got = total = 0.0
    for km, w in weights:
        total += w
        la, lo = (lat, lon) if km == 0 else destination(lat, lon, az, km)
        v = sky(la, lo, field)
        if v is not None:
            s += v * w
            got += w
    return (s / got if got else None), (got / total if total else 0.0)


def colour(canvas: float) -> float:
    """How much colour a cover of high and middle cloud (0-1) can give: best at about half."""
    if canvas <= 0.5:
        return 0.38 + 0.62 * (1 - ((canvas - 0.5) / 0.5) ** 2)
    return 1 - 0.45 * ((canvas - 0.5) / 0.5) ** 2


def rate(sky: Sky, lat: float, lon: float, az: float) -> dict[str, float] | None:
    """The score and its reasons at one place for one event."""
    high, _ = along(sky, lat, lon, az, "high", CANVAS_HIGH)
    mid, _ = along(sky, lat, lon, az, "mid", CANVAS_MID)
    if high is None and mid is None:
        return None
    high, mid = (high or 0.0) / 100, (mid or 0.0) / 100
    canvas = 1 - (1 - high) * (1 - 0.85 * mid)
    # How much light gets in under each layer.
    light, seen_w, seen_n = {}, 0.0, 0
    for layer, blockers in BLOCK.items():
        clear = 1.0
        for field, weights, strength in blockers:
            cover, share = along(sky, lat, lon, az, field, weights)
            seen_w += share
            seen_n += 1
            # What the model can't see counts as UNKNOWN.
            frac = (cover / 100 if cover is not None else UNKNOWN) * share + UNKNOWN * (1 - share)
            clear *= 1 - strength * frac
        light[layer] = max(0.0, clear) ** 1.3
    weight = high + 0.85 * mid
    glow = (high * light["high"] + 0.85 * mid * light["mid"]) / weight if weight > 0.02 else light["high"]
    low_near, _ = along(sky, lat, lon, az, "low", NEAR)
    low_near = (low_near or 0.0) / 100
    low_sun, _ = along(sky, lat, lon, az, "low", HORIZON_LOW)
    clean = 1 - 0.9 * ((low_sun or 0.0) / 100) ** 1.2  # the sun itself in view at the horizon
    s = min(1.0, canvas / 0.2)
    base = (1 - s) * 0.38 * clean + s * colour(canvas) * (0.12 + 0.88 * glow)
    hide = 0.85 * low_near ** 1.5
    vis, _ = along(sky, lat, lon, az, "vis", [(0, 0.6), (30, 0.4)])
    if vis is not None:
        haze = 0.3 + 0.7 * max(0.0, min(1.0, math.log10(max(vis, 0.1)) / math.log10(15)))
    else:
        rh = sky(lat, lon, "rh")
        haze = 1 - 0.5 * max(0.0, min(1.0, ((rh or 0) - 90) / 10))
    rain_here = sky(lat, lon, "rain") or 0.0
    rain_way, _ = along(sky, lat, lon, az, "rain", [(20, 0.5), (50, 0.5)])
    # Rain where you stand spoils it (a shower passing can still leave a rainbow); heavy rain on
    # the way in is thick cloud low down.
    wet = (0.45 if rain_here >= 1 else 0.65 if rain_here >= 0.3 else 0.85 if rain_here >= 0.1 else 1.0) \
        * (0.75 if (rain_way or 0) >= 0.5 else 1.0)
    score = 100 * base * (1 - hide) * haze * wet
    return {"score": round(max(0.0, min(100.0, score)), 1), "canvas": round(canvas * 100), "high": round(high * 100),
            "mid": round(mid * 100), "gap": round((glow if canvas >= 0.1 else clean) * 100),
            "low": round(low_near * 100), "vis": None if vis is None else round(vis, 1),
            "seen": round(100 * seen_w / seen_n) if seen_n else 0, "rain": round(rain_here, 1)}


# --- the run ------------------------------------------------------------------------------

def plan(points: list, first: int, last: int, now: float) -> list[tuple[int, str, list]]:
    """The events to score: [(time bar key, kind, [(time, azimuth, glow time) or None per point])]."""
    out = []
    for ref_t, kind, _ in events(REF[0], REF[1], max(now - 1800, first), last):
        mine = []
        for lat, lon in points:
            # The point's own event: within a few hours of Helsinki's.
            own = [e for e in events(lat, lon, ref_t - 3 * 3600, ref_t + 3 * 3600, step=1800) if e[1] == kind]
            if not own:
                mine.append(None)  # midnight sun or polar night
                continue
            t, _, az = min(own, key=lambda e: abs(e[0] - ref_t))
            mine.append((t, az, glow_time(lat, lon, t, kind)))
        out.append((int(round(ref_t / 3600) * 3600), kind, mine))
    return out


def hours_of(t: float) -> tuple[int, int]:
    h0 = int(t // 3600 * 3600)
    return h0, h0 + 3600


def score_event(b: RunBuilder, key: int, points: list, mine: list, grid: Grid, hours: dict[int, Any]) -> None:
    for i, ((lat, lon), ev) in enumerate(zip(points, mine)):
        if ev is None:
            continue
        t, az, glow = ev
        b.set(i, key, 1, round(az))
        b.set(i, key, 2, round((t - key) / 60))
        b.set(i, key, 3, round((glow - key) / 60))
        r = rate(Sky(grid, hours, t), lat, lon, az)
        if r is None:
            continue
        for name, v in r.items():
            b.set(i, key, FIELDS.index(name), v)


async def build_run(registry: Overlays, now: float) -> dict[str, Any]:
    run = await registry.run_for(FORECAST, now)
    if not run or not run.get("times"):
        raise ValueError("no weather model yet")
    first, last = run["times"][0], run["times"][-1]
    points = run["points"]
    evs = await asyncio.to_thread(plan, points, first, last, now)
    if not evs:
        raise ValueError("no sunset or sunrise in the forecast")
    grid = Grid(run)
    b = RunBuilder([k for k, _, _ in evs], FIELDS, points, SCALE)
    for key, _, mine in evs:
        # One event at a time, with only the model's hours around it (2 bytes a value).
        wanted = sorted({h for ev in mine if ev for h in hours_of(ev[0]) if first <= h <= last})
        hours = {h: await registry.raw_values(FORECAST, h) for h in wanted}
        await asyncio.to_thread(score_event, b, key, points, mine, grid, hours)
        del hours
    return b.run(min_share=0.05, info={"kinds": {str(k): kind for k, kind, _ in evs},
                                       "source": (run.get("info") or {}).get("source")})


def overlay(registry: Overlays, **changes: Any) -> Overlay:
    async def points(session: Any, now: float) -> dict[str, Any]:
        return await build_run(registry, now)

    async def value(lat: float, lon: float, ts: int) -> dict[str, Any] | None:
        """The coming sunsets and sunrises at the grid point nearest a place."""
        run = await registry.run_for(ID)
        if not run:
            return None
        kinds = (run.get("info") or {}).get("kinds", {})
        rows = await registry.series(ID, lat, lon, ["score", "at", "glow"], 0, 2 ** 40)
        out = [{"t": t, "kind": kinds.get(str(t), "sunset"), "score": score, "at": t + at * 60,
                "glow": t + glow * 60, "word": word(score)}
               for t, score, at, glow in rows if score is not None and at is not None and glow is not None]
        return {"events": out} if out else None

    kw: dict[str, Any] = dict(
        id=ID, title="Sunset & sunrise", kind="points", points=points, value=value, max_age=3 * 3600, refresh=1800,
        color="#f97316", icon="sunset", group="field", opacity=0.8, min_zoom=3, frame_ms=1500,
        bounds=registry.registry[FORECAST].bounds if FORECAST in registry.registry else None, valid_for=12 * 3600,
        attribution=ATTRIBUTION,
        note="How colourful each sunset and sunrise of the next two days is likely to be: cloud high up to catch "
             "the light, a clear way in for it under the clouds towards the sun, and no low cloud, fog or rain "
             "where you stand. Tap the map for why; zoom in for spots on land with an open view that way.",
    )
    kw.update(changes)
    return Overlay(**kw)


def register(registry: Overlays, **changes: Any) -> None:
    registry.register(overlay(registry, **changes))
