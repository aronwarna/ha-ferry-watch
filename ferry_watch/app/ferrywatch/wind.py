"""The wind forecast as a map overlay (card T5): Windy-style wind over the Gulf of Finland
for the next 48 hours.

FMI's HARMONIE forecast (run by FMI and its Nordic partners as MEPS) from the open WFS:
a point query (multipointcoverage) on a regular grid of 126 points, about 30 km apart,
asking wind speed, gust and direction for every hour of the next two days. One run is
fetched at most every 3 hours, only while a panel has the layer on, and kept as a small
file on disk (about 100 KB). The panel draws an arrow per point and animated wind lines
interpolated between them.

The grid is asked for in three requests of 42 points, so no answer is big (about 150 KB
of XML each). If FMI doesn't know the HARMONIE stored query any more, the MEPS one (its
newer name, same model) is tried.

The forecast is checked against FMI's coastal stations when they have been read anyway
(the Weather stations layer): Settings says how far apart the two are.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .config import USER_AGENT
from .overlays import Overlay, Overlays
from .stations import parse_coverage
from .weather import WFS

_LOGGER = logging.getLogger(__name__)

ID = "wind"
QUERIES = ("fmi::forecast::harmonie::surface::point::multipointcoverage",
           "fmi::forecast::meps::surface::point::multipointcoverage")
PARAMS = "WindSpeedMS,WindGust,WindDirection"
FIELDS = ["ms", "gust", "dir"]
HOURS = 48
RUN_EVERY = 3 * 3600  # s; MEPS runs every 3 hours (FMI publishes each about 2 hours later)
BATCH = 42  # grid points per request
TIMEOUT = 25
MAX_BYTES = 2 * 1024 * 1024  # per answer; 42 points x 49 hours is about 150 KB
MATCH = 0.06  # degrees: an answer's point this close to an asked one is that one
ATTRIBUTION = "Wind forecast: Finnish Meteorological Institute, HARMONIE (CC BY 4.0)"

# The grid: 7 rows of 18 points, south to north and west to east, from Hanko and the
# Estonian islands to Vyborg Bay (about 30 km apart: 0.3 degrees of latitude, 0.55 of
# longitude).
LATS = [round(59.0 + 0.3 * i, 2) for i in range(7)]
LONS = [round(21.0 + 0.55 * i, 2) for i in range(18)]
GRID = [(lat, lon) for lat in LATS for lon in LONS]
BOUNDS = (LATS[0], LONS[0], LATS[-1], LONS[-1])

CHECK_WINDOW = 5400  # s: station readings and forecast hours this far apart are compared
CHECK_STATIONS_AGE = 3600  # s: older station readings aren't used
CHECK_MIN = 3  # stations needed for a comparison

Fetch = Callable[[list[tuple[str, str]]], Awaitable[str]]


def request_params(query: str, points: list[tuple[float, float]], now: float) -> list[tuple[str, str]]:
    """A WFS GetFeature for one batch of points: the hour now started and the next 48."""
    start = int(now // 3600 * 3600)
    params = [("service", "WFS"), ("version", "2.0.0"), ("request", "getFeature"), ("storedquery_id", query),
              ("parameters", PARAMS), ("timestep", "60"),
              ("starttime", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))),
              ("endtime", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start + HOURS * 3600)))]
    return params + [("latlon", f"{lat:.2f},{lon:.2f}") for lat, lon in points]


def assign(rows: list[tuple[float, float, int, list[float | None]]],
           wanted: list[tuple[float, float]]) -> dict[int, list[tuple[int, list[float | None]]]]:
    """The answer's rows by the index of the asked point they belong to.

    Each row carries its point's coordinates: normally the asked ones, matched by
    distance. Should FMI give the coordinates of the model's grid cell or of a nearby
    place instead, but one point for each asked, they are taken in the asked order."""
    places: dict[tuple[float, float], list[tuple[int, list[float | None]]]] = {}
    for lat, lon, ts, values in rows:
        places.setdefault((round(lat, 4), round(lon, 4)), []).append((ts, values))
    out: dict[int, list[tuple[int, list[float | None]]]] = {}
    for (lat, lon), series in places.items():
        k = math.cos(math.radians(lat))
        best = min(range(len(wanted)), default=None,
                   key=lambda i: (wanted[i][0] - lat) ** 2 + ((wanted[i][1] - lon) * k) ** 2)
        if best is None or best in out:
            break
        if (wanted[best][0] - lat) ** 2 + ((wanted[best][1] - lon) * k) ** 2 > MATCH ** 2:
            break
        out[best] = series
    else:
        return out
    if len(places) == len(wanted):
        return {i: series for i, series in enumerate(places.values())}
    return out


def build_run(answers: list[tuple[list[str], list[tuple[float, float, int, list[float | None]]]]],
              batches: list[list[tuple[float, float]]]) -> dict[str, Any]:
    """A points run (see overlays.Overlay) from the parsed answers, one per batch: every
    hour that has values for at least half the grid."""
    names = [p.lower() for p in PARAMS.split(",")]
    by_time: dict[int, list[list[float | None]]] = {}
    points = [p for batch in batches for p in batch]
    first = 0
    for (fields, rows), batch in zip(answers, batches):
        order = [fields.index(n) if n in fields else None for n in names]
        for i, series in assign(rows, batch).items():
            for ts, values in series:
                slot = by_time.setdefault(ts, [[None] * len(FIELDS) for _ in points])
                slot[first + i] = [None if j is None else values[j] for j in order]
        first += len(batch)
    times = [t for t in sorted(by_time) if sum(v[0] is not None for v in by_time[t]) * 2 >= len(points)]
    if not times:
        raise ValueError("no wind values in the forecast")
    return {"times": times, "fields": FIELDS, "points": [list(p) for p in points],
            "values": {str(t): by_time[t] for t in times}}


async def fetch_run(session: aiohttp.ClientSession | None, now: float, fetch: Fetch | None = None,
                    queries: list[str] | None = None) -> dict[str, Any]:
    """The forecast for the whole grid, in batches; the stored query that answered goes to
    the front of `queries` for next time."""
    queries = queries if queries is not None else list(QUERIES)

    async def get(params: list[tuple[str, str]]) -> str:
        async with session.get(WFS, params=params, headers={"User-Agent": USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            resp.raise_for_status()
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("answer too big")
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    if fetch is None and session is None:
        raise ValueError("no connection")
    fetch = fetch or get
    batches = [GRID[i:i + BATCH] for i in range(0, len(GRID), BATCH)]
    err: Exception | None = None
    for query in list(queries):
        answers = []
        try:
            for batch in batches:  # one after the other: one answer in memory at a time
                text = await fetch(request_params(query, batch, now))
                answers.append(parse_coverage(text, PARAMS))
                del text
        except aiohttp.ClientResponseError as e:
            # 400: FMI doesn't know this stored query (or a parameter): try the other name.
            err = e
            if e.status != 400:
                raise
            _LOGGER.debug("Wind forecast: %s refused (%s)", query, e.status)
            continue
        run = build_run(answers, batches)
        if query != queries[0]:
            queries.remove(query)
            queries.insert(0, query)
        return run
    raise err or ValueError("no stored query answered")


# --- checking the forecast against the stations --------------------------------------------

def _grid(run: dict[str, Any]) -> tuple[list[float], list[float], dict[tuple[float, float], int]]:
    index = {(p[0], p[1]): i for i, p in enumerate(run["points"])}
    return sorted({p[0] for p in run["points"]}), sorted({p[1] for p in run["points"]}), index


def _cell(axis: list[float], v: float) -> tuple[int, float] | None:
    """The grid step v falls in and how far along it (0..1); None outside the grid."""
    if len(axis) < 2 or not axis[0] <= v <= axis[-1]:
        return None
    i = 0
    while i < len(axis) - 2 and v > axis[i + 1]:
        i += 1
    return i, (v - axis[i]) / (axis[i + 1] - axis[i])


def speed_at(run: dict[str, Any], ts: int, lat: float, lon: float, grid: tuple | None = None) -> float | None:
    """The forecast wind speed at a place and forecast hour, interpolated between the four
    grid points around it (the ones that have a value); None outside the grid."""
    lats, lons, index = grid or _grid(run)
    rows = run["values"].get(str(ts))
    a, b = _cell(lats, lat), _cell(lons, lon)
    if rows is None or a is None or b is None:
        return None
    (i, fy), (j, fx) = a, b
    total = weight = 0.0
    for di, wy in ((0, 1 - fy), (1, fy)):
        for dj, wx in ((0, 1 - fx), (1, fx)):
            k = index.get((lats[i + di], lons[j + dj]))
            v = rows[k][0] if k is not None else None
            if v is not None and wy * wx > 0:
                total += v * wy * wx
                weight += wy * wx
    return total / weight if weight else None


def compare(run: dict[str, Any], stations: list[dict[str, Any]], now: float) -> dict[str, Any] | None:
    """How far the forecast is from the stations' latest wind readings, at the forecast hour
    nearest each reading: {"n", "mean" (average difference, m/s), "bias" (forecast minus
    stations)}; None with too few stations to say."""
    grid = _grid(run)
    times = run["times"]
    diffs = []
    for s in stations:
        ws, ts = s.get("ws"), s.get("wind_ts") or s.get("ts")
        if ws is None or not ts or now - ts > CHECK_STATIONS_AGE or not times:
            continue
        hour = min(times, key=lambda t: abs(t - ts))
        if abs(hour - ts) > CHECK_WINDOW:
            continue
        f = speed_at(run, hour, s["lat"], s["lon"], grid)
        if f is not None:
            diffs.append(f - ws)
    if len(diffs) < CHECK_MIN:
        return None
    return {"n": len(diffs), "mean": round(sum(abs(d) for d in diffs) / len(diffs), 1),
            "bias": round(sum(diffs) / len(diffs), 1)}


def check_text(result: dict[str, Any] | None) -> str | None:
    if not result:
        return None
    bias = result["bias"]
    lean = (f", the forecast {abs(bias):.1f} m/s {'stronger' if bias > 0 else 'weaker'} overall"
            if abs(bias) >= 0.5 else "")
    return (f"Wind forecast against {result['n']} FMI stations' latest readings: "
            f"{result['mean']:.1f} m/s apart on average{lean}.")


# --- the overlay --------------------------------------------------------------------------

def overlay(stations: Any = None, fetch: Fetch | None = None, **changes: Any) -> Overlay:
    queries = list(QUERIES)

    async def points(session: aiohttp.ClientSession | None, now: float) -> dict[str, Any]:
        return await fetch_run(session, now, fetch, queries)

    def check(run: dict[str, Any], now: float) -> str | None:
        # Only the readings the Weather stations layer fetched anyway: nothing extra is asked.
        if stations is None or not stations.stations or now - stations.fetched > CHECK_STATIONS_AGE:
            return None
        return check_text(compare(run, list(stations.stations.values()), now))

    kw: dict[str, Any] = dict(
        id=ID, title="Wind forecast", kind="points", points=points, check=check,
        max_age=RUN_EVERY, refresh=1800, frame_ms=700, opacity=0.9, color="#0f766e",
        min_zoom=5, bounds=BOUNDS, valid_for=3600, attribution=ATTRIBUTION,
        note="FMI's HARMONIE forecast for the next 48 hours: arrows where the wind blows, coloured by "
             "speed in m/s, and moving wind lines. Step through the hours on the time bar.",
    )
    kw.update(changes)
    return Overlay(**kw)


def register(overlays: Overlays, stations: Any = None, fetch: Fetch | None = None, **changes: Any) -> None:
    overlays.register(overlay(stations, fetch, **changes))
