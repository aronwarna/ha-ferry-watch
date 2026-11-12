"""The boating day score: is it a good hour to set off on a route in a small boat?

For each whole hour of the next two days the route is followed at the boat's speed and the
forecast read where the boat would be, when it would be there: wind, gusts, waves, rain,
visibility and daylight. Each counts as good, okay or bad against the limits of the chosen
boat (boats.py), and the hour takes the worst of them all along the way, with the reasons and
where on the route they bite.

The weather comes for the route's own points rather than the colour layers' 33 km grid:
FMI's HARMONIE point forecast (2.5 km model) for the air, Open-Meteo's marine forecast
(which picks the nearest sea cell of the finest wave model it has) for the waves, both
asked for once an hour at most for a route, a few dozen points in one request each. If
either doesn't answer, the colour layers' runs (the nearest grid point) stand in.

Waves in the inner archipelago: the wave models see the open sea best. Where the nearest
sea cell they answer for is more than a couple of km from the route, the route is
sheltered by islands the model doesn't know, and its waves are counted at less (half
from 8 km away). A guess, said in the reasons.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from bisect import bisect_right
from typing import Any, Awaitable, Callable

import aiohttp

from . import boats, forecast, marine
from .geo import distance_m
from .stations import parse_coverage
from .sunset import sun_position
from .weather import WFS

_LOGGER = logging.getLogger(__name__)

SAMPLE_M = 3000.0  # a point along the route at least this often...
MAX_SAMPLES = 30  # ...but no more than these (a long route gets them further apart)
HOURS = 48
CACHE_S = 3600  # a route's forecast is asked for again after an hour
FAIL_S = 300  # a failed source is tried again after five minutes
CACHE_KEEP = 8  # routes' forecasts kept in memory
KN = 1852 / 3600  # m/s in a knot
DARK_SUN = -6.0  # the sun below this (civil dusk) counts as dark
SHELTER_FROM_KM, SHELTER_FULL_KM, SHELTER_MIN = 2.0, 8.0, 0.5

# Each boat's limits are in boats.py: (okay from, bad from); visibility the other way round.
BOATS = boats.BOATS
DEFAULT_BOAT = boats.DEFAULT_BOAT

FMI_PARAMS = [("WindSpeedMS", "ms"), ("WindGust", "gust"), ("WindDirection", "dir"), ("Precipitation1h", "rain"),
              ("Temperature", "t"), ("Visibility", "vis")]
OM_PARAMS = {"ms": "wind_speed_10m", "gust": "wind_gusts_10m", "dir": "wind_direction_10m", "rain": "precipitation",
             "t": "temperature_2m", "vis": "visibility"}
SEA_PARAMS = {"wave": "wave_height", "wdir": "wave_direction", "period": "wave_period"}
AIR = [f for _, f in FMI_PARAMS]
SEA = list(SEA_PARAMS)

Fetch = Callable[[str, list[tuple[str, str]]], Awaitable[str]]
GetJson = Callable[[str, dict[str, str]], Awaitable[Any]]


# --- the route --------------------------------------------------------------------------

def sample(points: list[list[float]]) -> list[tuple[float, float, float]]:
    """Points along the route, (lat, lon, metres from the start): the start, the end, and
    evenly between, SAMPLE_M apart or further on a long route."""
    pts = [(float(p[0]), float(p[1])) for p in points]
    seg = [distance_m(*a, *b) for a, b in zip(pts, pts[1:])]
    total = sum(seg)
    if not pts:
        return []
    if total <= 0:
        return [(pts[0][0], pts[0][1], 0.0)]
    step = max(SAMPLE_M, total / (MAX_SAMPLES - 1))
    n = max(1, math.ceil(total / step - 1e-9))
    out, i, walked = [], 0, 0.0
    for k in range(n + 1):
        want = total * k / n
        while i < len(seg) - 1 and walked + seg[i] < want:
            walked += seg[i]
            i += 1
        f = 0.0 if seg[i] <= 0 else min(1.0, max(0.0, (want - walked) / seg[i]))
        (a_lat, a_lon), (b_lat, b_lon) = pts[i], pts[i + 1]
        out.append((round(a_lat + (b_lat - a_lat) * f, 5), round(a_lon + (b_lon - a_lon) * f, 5), want))
    return out


# --- the forecast along it ---------------------------------------------------------------

class Series:
    """Hourly values per point: times, and per field a list (per point) of lists (per hour)."""

    def __init__(self, times: list[int], fields: list[str], n: int):
        self.times = times
        self.fields = fields
        self.values = {f: [[None] * len(times) for _ in range(n)] for f in fields}
        self.extra: dict[str, Any] = {}

    def set(self, i: int, ts: int, field: str, v: float | None) -> None:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return
        try:
            j = self.times.index(int(ts))
        except ValueError:
            return
        self.values[field][i][j] = float(v)

    def has(self, field: str) -> bool:
        return any(v is not None for row in self.values[field] for v in row)

    def at(self, field: str, i: int, ts: float) -> float | None:
        """A point's value at any moment, straight between the hours either side."""
        row = self.values[field][i]
        times = self.times
        if not times or ts < times[0] or ts > times[-1]:
            return None
        j = min(len(times) - 1, bisect_right(times, ts) - 1)
        a = row[j]
        if j + 1 >= len(times) or ts == times[j]:
            return a
        b = row[j + 1]
        if a is None or b is None:
            return a if b is None else b
        f = (ts - times[j]) / (times[j + 1] - times[j])
        if field in ("dir", "wdir"):
            d = (b - a + 540) % 360 - 180
            return (a + d * f) % 360
        return a + (b - a) * f


def hours(now: float) -> list[int]:
    start = int(now // 3600 * 3600)
    return [start + i * 3600 for i in range(HOURS + 1)]


def _match(rows_at: list[tuple[float, float]], asked: list[tuple[float, float, float]]) -> dict[tuple, int]:
    """Answer coordinates to the asked points: the nearest within ~7 km each, or by order."""
    out: dict[tuple, int] = {}
    for key in rows_at:
        k = math.cos(math.radians(key[0]))
        best = min(range(len(asked)), key=lambda i: (asked[i][0] - key[0]) ** 2 + ((asked[i][1] - key[1]) * k) ** 2)
        if (asked[best][0] - key[0]) ** 2 + ((asked[best][1] - key[1]) * k) ** 2 > forecast.MATCH ** 2:
            return {key: n for n, key in enumerate(rows_at)} if len(rows_at) == len(asked) else {}
        out[key] = best
    return out


async def air_fmi(pts: list[tuple[float, float, float]], now: float, fetch: Fetch) -> Series:
    err: Exception | None = None
    for query in forecast.QUERIES:
        for names in (FMI_PARAMS, FMI_PARAMS[:-1]):
            try:
                text = await fetch(WFS, forecast.request_params(query, [n for n, _ in names],
                                                                [(p[0], p[1]) for p in pts], now))
            except aiohttp.ClientResponseError as e:
                err = e
                if e.status != 400:
                    raise
                continue
            fields, rows = parse_coverage(text, ",".join(n for n, _ in names))
            s = Series(hours(now), AIR, len(pts))
            seen: list[tuple[float, float]] = []
            for lat, lon, _, _ in rows:
                key = (round(lat, 4), round(lon, 4))
                if key not in seen:
                    seen.append(key)
            index = _match(seen, pts)
            for lat, lon, ts, values in rows:
                i = index.get((round(lat, 4), round(lon, 4)))
                if i is None:
                    continue
                for fmi, ours in names:
                    if fmi.lower() in fields:
                        v = values[fields.index(fmi.lower())]
                        s.set(i, ts, ours, v / 1000 if v is not None and ours == "vis" else v)
            if s.has("ms"):
                s.extra["air"] = "fmi"
                return s
            err = ValueError("no wind in FMI's answer")
    raise err or ValueError("no stored query answered")


async def air_open_meteo(pts: list[tuple[float, float, float]], now: float, get_json: GetJson) -> Series:
    params = {"latitude": ",".join(f"{p[0]:.4f}" for p in pts), "longitude": ",".join(f"{p[1]:.4f}" for p in pts),
              "hourly": ",".join(OM_PARAMS.values()), "wind_speed_unit": "ms", "timeformat": "unixtime",
              "forecast_hours": str(HOURS + 1), "timezone": "GMT"}
    answer = await get_json(forecast.OPEN_METEO, params)
    places = answer if isinstance(answer, list) else [answer]
    s = Series(hours(now), AIR, len(pts))
    for i, place in enumerate(places[:len(pts)]):
        hourly = place.get("hourly") or {}
        for f, name in OM_PARAMS.items():
            for ts, v in zip(hourly.get("time") or [], hourly.get(name) or []):
                s.set(i, int(ts), f, v / 1000 if v is not None and f == "vis" else v)
    if not s.has("ms"):
        raise ValueError("no wind in Open-Meteo's answer")
    s.extra["air"] = "open-meteo"
    return s


async def sea_open_meteo(pts: list[tuple[float, float, float]], now: float, get_json: GetJson) -> Series:
    params = {"latitude": ",".join(f"{p[0]:.4f}" for p in pts), "longitude": ",".join(f"{p[1]:.4f}" for p in pts),
              "hourly": ",".join(SEA_PARAMS.values()), "timeformat": "unixtime", "timezone": "GMT",
              "forecast_hours": str(HOURS + 1), "cell_selection": "sea"}
    answer = await get_json(marine.URL, params)
    places = answer if isinstance(answer, list) else [answer]
    s = Series(hours(now), SEA, len(pts))
    cell_km: list[float | None] = [None] * len(pts)
    for i, place in enumerate(places[:len(pts)]):
        hourly = place.get("hourly") or {}
        for f, name in SEA_PARAMS.items():
            for ts, v in zip(hourly.get("time") or [], hourly.get(name) or []):
                s.set(i, int(ts), f, v)
        try:
            cell_km[i] = distance_m(pts[i][0], pts[i][1], float(place["latitude"]), float(place["longitude"])) / 1000
        except (KeyError, TypeError, ValueError):
            pass
    s.extra["cell_km"] = cell_km
    s.extra["sea"] = "open-meteo"
    return s


async def from_run(overlays: Any, run_id: str, fields: list[str], pts: list[tuple[float, float, float]],
                   now: float) -> Series | None:
    """The colour layers' run instead: each route point's nearest grid point, hour by hour."""
    if overlays is None or run_id not in overlays.registry:
        return None
    run = await overlays.run_for(run_id, now)
    if not run or not run.get("points"):
        return None
    grid = run["points"]
    near, km = [], []
    for lat, lon, _ in pts:
        k = math.cos(math.radians(lat))
        j = min(range(len(grid)), key=lambda g: (grid[g][0] - lat) ** 2 + ((grid[g][1] - lon) * k) ** 2)
        near.append(j)
        km.append(distance_m(lat, lon, grid[j][0], grid[j][1]) / 1000)
    pick = {f: run["fields"].index(f) for f in fields if f in run["fields"]}
    times = [int(t) for t in run["times"] if now - 3600 <= t <= now + HOURS * 3600]
    s = Series(times, fields, len(pts))
    for t in times:
        rows = await overlays.values_at(run_id, t)
        if not rows:
            continue
        for i, j in enumerate(near):
            for f, c in pick.items():
                s.set(i, t, f, rows[j][c])
    s.extra["cell_km"] = km
    return s if any(s.has(f) for f in pick) else None


class DayScore:
    """Keeps each route's forecast for an hour; scores departures from it on demand."""

    def __init__(self, session: aiohttp.ClientSession | None, overlays: Any = None,
                 fetch: Fetch | None = None, get_json: GetJson | None = None):
        text, js = forecast.http_getters(session)
        self.fetch = fetch or text
        self.get_json = get_json or js
        self.overlays = overlays
        self._cache: dict[tuple, tuple[float, Series | None, Series | None, dict[str, str]]] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._failed: dict[str, float] = {}

    async def _try(self, name: str, now: float, fn: Callable[[], Awaitable[Series]]) -> tuple[Series | None, str | None]:
        if now - self._failed.get(name, -1e9) < FAIL_S:
            return None, "tried a moment ago"
        try:
            return await fn(), None
        except Exception as err:  # noqa: BLE001 - the next source usually still works
            self._failed[name] = now
            _LOGGER.info("Day score: %s failed: %s", name, err)
            return None, str(err) or type(err).__name__

    async def conditions(self, pts: list[tuple[float, float, float]], now: float | None = None):
        now = now or time.time()
        key = tuple((round(p[0], 3), round(p[1], 3)) for p in pts)
        hit = self._cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            return hit[1:]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < CACHE_S:
                return hit[1:]
            errors: dict[str, str] = {}
            air, e = await self._try("fmi", now, lambda: air_fmi(pts, now, self.fetch))
            if air is None:
                errors["fmi"] = e or ""
                air, e = await self._try("open-meteo", now, lambda: air_open_meteo(pts, now, self.get_json))
            if air is None:
                errors["open-meteo"] = e or ""
                air = await from_run(self.overlays, forecast.RUN_ID, AIR, pts, now)
                if air:
                    air.extra["air"] = "layers"
            sea, e = await self._try("marine", now, lambda: sea_open_meteo(pts, now, self.get_json))
            if sea is None:
                errors["marine"] = e or ""
                sea = await from_run(self.overlays, marine.RUN_ID, SEA, pts, now)
                if sea:
                    sea.extra["sea"] = "layers"
            if air is not None:  # nothing to keep without the wind: ask again next time
                self._cache[key] = (now, air, sea, errors)
                while len(self._cache) > CACHE_KEEP:
                    self._cache.pop(next(iter(self._cache)))
            self._locks.pop(key, None)
            return air, sea, errors

    async def score(self, points: list[list[float]], speed_kn: float, boat: str = DEFAULT_BOAT,
                    now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        pts = sample(points)
        if len(pts) < 2:
            raise ValueError("A route needs two points")
        air, sea, errors = await self.conditions(pts, now)
        return score_hours(pts, air, sea, speed_kn, boat, now, errors)


# --- the score --------------------------------------------------------------------------

def level(kind: str, v: float | None, limits: dict[str, Any]) -> int:
    if v is None:
        return 0
    okay, bad = limits[kind]
    if kind == "vis":
        return 2 if v < bad else 1 if v < okay else 0
    return 2 if v >= bad else 1 if v >= okay else 0


def shelter(cell_km: float | None) -> float:
    """How much of the wave model's height counts where its nearest sea cell is this far off."""
    if cell_km is None or cell_km <= SHELTER_FROM_KM:
        return 1.0
    f = min(1.0, (cell_km - SHELTER_FROM_KM) / (SHELTER_FULL_KM - SHELTER_FROM_KM))
    return 1.0 - (1.0 - SHELTER_MIN) * f


def score_hours(pts: list[tuple[float, float, float]], air: Series | None, sea: Series | None, speed_kn: float,
                boat: str, now: float, errors: dict[str, str] | None = None) -> dict[str, Any]:
    boat, limits = boats.boat(boat)
    total = pts[-1][2]
    speed = max(0.5, float(speed_kn)) * KN
    trip = total / speed
    cell_km = (sea.extra.get("cell_km") if sea else None) or [None] * len(pts)
    out: list[dict[str, Any]] = []
    if air is not None and air.times:
        first = max(air.times[0], int(now // 3600 * 3600))
        for dep in range(first, air.times[-1] + 1, 3600):
            if dep + trip > air.times[-1]:
                break
            out.append(_hour(pts, air, sea, cell_km, dep, speed, limits))
    return {
        "boat": boat,
        "boats": {k: v["title"] for k, v in BOATS.items()},
        "limits": {k: limits[k] for k in ("wind", "gust", "wave", "rain", "vis")},
        "nm": round(total / 1852, 2), "trip_s": round(trip),
        "samples": [[p[0], p[1], round(p[2] / 1852, 2)] for p in pts],
        "hours": out,
        "sources": {"air": (air.extra.get("air") if air else None), "sea": (sea.extra.get("sea") if sea else None)},
        "errors": errors or {},
        "updated": int(now),
    }


def _hour(pts, air: Series, sea: Series | None, cell_km, dep: int, speed: float, limits) -> dict[str, Any]:
    worst: dict[str, tuple[float, int]] = {}  # kind -> (value, sample index) at its worst
    levels: list[int] = []
    temp: float | None = None
    dark = False
    sheltered = False
    for i, (lat, lon, d) in enumerate(pts):
        ts = dep + d / speed
        here = 0
        vals = {"wind": air.at("ms", i, ts), "gust": air.at("gust", i, ts), "rain": air.at("rain", i, ts),
                "vis": air.at("vis", i, ts)}
        if sea is not None:
            w = sea.at("wave", i, ts)
            if w is not None:
                k = shelter(cell_km[i] if i < len(cell_km) else None)
                sheltered |= k < 1
                vals["wave"] = w * k
        t = air.at("t", i, ts)
        if t is not None:
            temp = t if temp is None else min(temp, t)
        for kind, v in vals.items():
            if v is None:
                continue
            here = max(here, level(kind, v, limits))
            prev = worst.get(kind)
            if prev is None or (v < prev[0] if kind == "vis" else v > prev[0]):
                worst[kind] = (v, i)
        if sun_position(lat, lon, ts)[0] < DARK_SUN:
            dark = True
            here = max(here, 1)
        levels.append(here)
    why = []
    for kind, (v, i) in worst.items():
        lv = level(kind, v, limits)
        if lv:
            why.append({"k": kind, "v": round(v, 2 if kind == "wave" else 1), "lvl": lv, "nm": round(pts[i][2] / 1852, 1)})
    if dark:
        why.append({"k": "dark", "lvl": 1})
    why.sort(key=lambda r: -r["lvl"])
    score = max(levels) if levels else 0
    at = max(range(len(levels)), key=lambda i: levels[i]) if levels else 0
    return {
        "t": dep, "score": score, "levels": "".join(str(v) for v in levels),
        **{k: round(v, 2 if k == "wave" else 1) for k, (v, _) in worst.items()},
        **({"temp": round(temp, 1)} if temp is not None else {}),
        **({"dir": round(air.at("dir", at, dep + pts[at][2] / speed) or 0)} if air.has("dir") else {}),
        "dark": dark, "sheltered": sheltered, "why": why, "at": at,
    }
