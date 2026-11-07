"""The weather model behind the Windy-style colour layers: wind, temperature, gusts, clouds,
rain, fog and pressure over southern and central Finland, the Gulf of Finland, Estonia and
the Stockholm archipelago, for the next 48 hours.

FMI's HARMONIE forecast (MEPS) from the open WFS, the same point query the wind layer
uses: a regular grid of 725 points about 33 km apart (0.3 degrees of latitude, 0.6 of
longitude), over land and sea alike, in requests of 42 points one after the other. One run
is fetched at most every 3 hours, only while a panel shows one of its layers, and kept as
one small binary file per forecast hour (about 17 KB each), so the NAS never holds more
than the hour a panel asks for.

If FMI doesn't answer (or knows neither stored query), the same fields come from
Open-Meteo's forecast API instead (free for non-commercial use, CC BY 4.0), which blends
the same Nordic HARMONIE model with others.

The run is registered once, hidden; each colour layer is an overlay sharing it and sending
the panel only its own fields.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .config import USER_AGENT
from .overlays import Overlay, Overlays, RunBuilder
from .stations import parse_coverage
from .weather import WFS

_LOGGER = logging.getLogger(__name__)

RUN_ID = "forecast"
QUERIES = ("fmi::forecast::harmonie::surface::point::multipointcoverage",
           "fmi::forecast::meps::surface::point::multipointcoverage")
# FMI's names, then ours. Visibility is asked for first; if FMI refuses it, without.
FMI_PARAMS = [("Temperature", "t"), ("Pressure", "p"), ("WindSpeedMS", "ms"), ("WindGust", "gust"),
              ("WindDirection", "dir"),
              ("TotalCloudCover", "cloud"), ("LowCloudCover", "low"), ("MediumCloudCover", "mid"),
              ("HighCloudCover", "high"), ("Precipitation1h", "rain"), ("Humidity", "rh"),
              ("DewPoint", "td"), ("Visibility", "vis")]
FIELDS = [f for _, f in FMI_PARAMS]
# Values are kept as tenths, pressure too (10132 for 1013.2 hPa); visibility as km.
SCALE = [10.0] * len(FIELDS)
SCALE[FIELDS.index("dir")] = 1.0  # whole degrees
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OM_PARAMS = {"t": "temperature_2m", "p": "pressure_msl", "ms": "wind_speed_10m", "gust": "wind_gusts_10m",
             "dir": "wind_direction_10m",
             "cloud": "cloud_cover", "low": "cloud_cover_low", "mid": "cloud_cover_mid", "high": "cloud_cover_high",
             "rain": "precipitation", "rh": "relative_humidity_2m", "td": "dew_point_2m", "vis": "visibility"}
HOURS = 48
RUN_EVERY = 3 * 3600
BATCH = 42
OM_BATCH = 100
TIMEOUT = 30
MAX_BYTES = 4 * 1024 * 1024
MATCH = 0.06
ATTRIBUTION = "Weather: Finnish Meteorological Institute, HARMONIE (CC BY 4.0)"
OM_ATTRIBUTION = "Weather: Open-Meteo.com (CC BY 4.0)"

LATS = [round(57.6 + 0.3 * i, 2) for i in range(29)]
LONS = [round(17.0 + 0.6 * j, 2) for j in range(25)]
GRID = [(lat, lon) for lat in LATS for lon in LONS]
BOUNDS = (LATS[0], LONS[0], LATS[-1], LONS[-1])

Fetch = Callable[[str, list[tuple[str, str]]], Awaitable[str]]


def request_params(query: str, names: list[str], points: list[tuple[float, float]],
                   now: float) -> list[tuple[str, str]]:
    start = int(now // 3600 * 3600)
    params = [("service", "WFS"), ("version", "2.0.0"), ("request", "getFeature"), ("storedquery_id", query),
              ("parameters", ",".join(names)), ("timestep", "60"),
              ("starttime", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))),
              ("endtime", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start + HOURS * 3600)))]
    return params + [("latlon", f"{lat:.2f},{lon:.2f}") for lat, lon in points]


def hours(now: float) -> list[int]:
    start = int(now // 3600 * 3600)
    return [start + i * 3600 for i in range(HOURS + 1)]


def _nearest(lat: float, lon: float, wanted: list[tuple[float, float]]) -> int | None:
    k = math.cos(math.radians(lat))
    best = min(range(len(wanted)), key=lambda i: (wanted[i][0] - lat) ** 2 + ((wanted[i][1] - lon) * k) ** 2)
    if (wanted[best][0] - lat) ** 2 + ((wanted[best][1] - lon) * k) ** 2 > MATCH ** 2:
        return None
    return best


def add_answer(b: RunBuilder, first: int, batch: list[tuple[float, float]], fields: list[str],
               rows: list[tuple[float, float, int, list[float | None]]], names: list[tuple[str, str]]) -> None:
    """One FMI answer into the run: rows matched to the asked points by their coordinates
    (or, if FMI gives other coordinates but one place per point, in the asked order)."""
    order = [(FIELDS.index(ours), fields.index(fmi.lower())) for fmi, ours in names if fmi.lower() in fields]
    places: dict[tuple[float, float], int] = {}
    seen: list[tuple[float, float]] = []
    for lat, lon, _, _ in rows:
        key = (round(lat, 4), round(lon, 4))
        if key not in places:
            places[key] = len(seen)
            seen.append(key)
    index: dict[tuple[float, float], int] = {}
    for key in seen:
        i = _nearest(key[0], key[1], batch)
        if i is None or i in index.values():
            index = {key: n for n, key in enumerate(seen)} if len(seen) == len(batch) else {}
            break
        index[key] = i
    for lat, lon, ts, values in rows:
        i = index.get((round(lat, 4), round(lon, 4)))
        if i is None:
            continue
        for ours, theirs in order:
            v = values[theirs]
            if v is not None and FIELDS[ours] == "vis":
                v = v / 1000  # m -> km
            b.set(first + i, ts, ours, v)


async def fetch_fmi(session: aiohttp.ClientSession | None, now: float, fetch: Fetch,
                    queries: list[str]) -> dict[str, Any]:
    batches = [GRID[i:i + BATCH] for i in range(0, len(GRID), BATCH)]
    err: Exception | None = None
    for query in list(queries):
        for names in (FMI_PARAMS, FMI_PARAMS[:-1]):
            b = RunBuilder(hours(now), FIELDS, GRID, SCALE)
            first = 0
            try:
                for batch in batches:
                    text = await fetch(WFS, request_params(query, [n for n, _ in names], batch, now))
                    fields, rows = parse_coverage(text, ",".join(n for n, _ in names))
                    del text
                    add_answer(b, first, batch, fields, rows, names)
                    first += len(batch)
            except aiohttp.ClientResponseError as e:
                err = e
                if e.status != 400:
                    raise
                _LOGGER.debug("Weather model: %s with %s refused", query, names[-1][0])
                continue
            if query != queries[0]:
                queries.remove(query)
                queries.insert(0, query)
            return b.run(info={"source": "fmi", "vis": len(names) == len(FMI_PARAMS)})
    raise err or ValueError("no stored query answered")


def add_open_meteo(b: RunBuilder, first: int, answer: Any) -> None:
    """One Open-Meteo answer (a list of locations, or one) into the run."""
    places = answer if isinstance(answer, list) else [answer]
    for n, place in enumerate(places):
        hourly = place.get("hourly") or {}
        times = hourly.get("time") or []
        for f, name in OM_PARAMS.items():
            column = hourly.get(name)
            if not column:
                continue
            j = FIELDS.index(f)
            for ts, v in zip(times, column):
                if v is not None and f == "vis":
                    v = v / 1000
                b.set(first + n, int(ts), j, v)


async def fetch_open_meteo(now: float, get_json: Callable[[str, dict[str, str]], Awaitable[Any]]) -> dict[str, Any]:
    b = RunBuilder(hours(now), FIELDS, GRID, SCALE)
    for i in range(0, len(GRID), OM_BATCH):
        batch = GRID[i:i + OM_BATCH]
        params = {"latitude": ",".join(f"{p[0]:.2f}" for p in batch),
                  "longitude": ",".join(f"{p[1]:.2f}" for p in batch),
                  "hourly": ",".join(OM_PARAMS.values()), "wind_speed_unit": "ms", "timeformat": "unixtime",
                  "forecast_hours": str(HOURS + 1), "timezone": "GMT"}
        add_open_meteo(b, i, await get_json(OPEN_METEO, params))
    return b.run(info={"source": "open-meteo", "vis": True})


def http_getters(session: aiohttp.ClientSession | None):
    async def text(url: str, params: Any) -> str:
        if session is None:
            raise ValueError("no connection")
        async with session.get(url, params=params, headers={"User-Agent": USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            resp.raise_for_status()
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("answer too big")
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    async def json_(url: str, params: Any) -> Any:
        import json
        return json.loads(await text(url, params))

    return text, json_


async def fetch_run(session: aiohttp.ClientSession | None, now: float, fetch: Fetch | None = None,
                    get_json: Callable | None = None, queries: list[str] | None = None) -> dict[str, Any]:
    """FMI first; Open-Meteo if FMI fails in any way."""
    text, js = http_getters(session)
    queries = queries if queries is not None else list(QUERIES)
    try:
        return await fetch_fmi(session, now, fetch or text, queries)
    except Exception as err:  # noqa: BLE001 - the other source usually still works
        _LOGGER.info("Weather model: FMI failed (%s), trying Open-Meteo", err)
        try:
            return await fetch_open_meteo(now, get_json or js)
        except Exception as err2:  # noqa: BLE001
            raise ValueError(f"FMI: {err}; Open-Meteo: {err2}") from err2


# --- the layers ---------------------------------------------------------------------------

LAYERS = [
    # id, title, fields sent, colour, icon, note
    ("temperature", "Temperature", ["t"], "#f97316", "temp",
     "Air temperature at 2 m from the weather model, hour by hour for two days."),
    ("gusts", "Wind gusts", ["gust", "ms"], "#7c3aed", "gust",
     "The strongest gusts expected in each hour, in m/s."),
    ("clouds", "Clouds", ["cloud", "low", "mid", "high"], "#64748b", "cloud",
     "Total cloud cover; tap the map for low, middle and high clouds."),
    ("rain", "Rain and snow", ["rain", "t"], "#2563eb", "rain",
     "Forecast precipitation per hour (mm), snow where it's below freezing. The rain radar shows what falls now."),
    ("fog", "Fog and visibility", ["vis", "rh", "td", "t", "ms", "low"], "#94a3b8", "fog",
     "How far you can see. Where the model has no visibility, the fog risk from humidity and low cloud."),
    ("pressure", "Pressure (isobars)", ["p"], "#475569", "pressure",
     "Air pressure at sea level as isobars every 2 hPa, with the highs and lows marked."),
]


def overlays(fetch: Fetch | None = None, get_json: Callable | None = None, **changes: Any) -> list[Overlay]:
    queries = list(QUERIES)

    async def points(session: aiohttp.ClientSession | None, now: float) -> dict[str, Any]:
        return await fetch_run(session, now, fetch, get_json, queries)

    base = Overlay(id=RUN_ID, title="Weather model", kind="points", points=points, hidden=True,
                   max_age=RUN_EVERY, refresh=1800, attribution=ATTRIBUTION, bounds=BOUNDS, valid_for=3600,
                   **changes)
    out = [base]
    for oid, title, fields, color, icon, note in LAYERS:
        out.append(Overlay(id=oid, title=title, kind="points", run_of=RUN_ID, fields=fields, color=color,
                           icon=icon, note=note, group="top" if oid == "pressure" else "field",
                           max_age=RUN_EVERY, refresh=1800, frame_ms=700, opacity=0.75 if oid != "pressure" else 0.9,
                           min_zoom=3, bounds=BOUNDS, valid_for=3600, attribution=ATTRIBUTION))
    return out


def register(registry: Overlays, fetch: Fetch | None = None, get_json: Callable | None = None, **changes: Any) -> None:
    for o in overlays(fetch, get_json, **changes):
        registry.register(o)
