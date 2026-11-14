"""Weather stations on the map: wind, gusts, direction and air temperature at every FMI
coastal and sea station, with sea temperature and wave height from the mareographs and wave
buoys, from the Finnish Meteorological Institute's open data (CC BY 4.0, no key needed).

Only the panel asks, while its "Weather stations" switch is on, and the answer is kept for
10 minutes: one bbox request covers all weather stations (multipointcoverage, the compact
format: one table for all stations), and two small ones the few buoys and mareographs. The
24-hour wind history behind the sparkline is one more bbox request at hourly steps, made
only when someone opens a station and kept for an hour. What is kept is small: a dozen
numbers per station and the history as short arrays (about 100 KB for 150 stations).
A failed request keeps the previous values and is logged once.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import xml.etree.ElementTree as ET
from array import array
from typing import Any, Awaitable, Callable

import aiohttp

from .coast import near_sea
from .config import USER_AGENT
from .geo import distance_m
from .weather import WFS, _local

_LOGGER = logging.getLogger(__name__)

BBOX = "19,59.3,30,66"  # lon/lat: all Finnish coastal and sea waters
WEATHER_QUERY = "fmi::observations::weather::multipointcoverage"
WEATHER_PARAMS = "windspeedms,windgust,winddirection,temperature"
HISTORY_PARAMS = "windspeedms,windgust"
WAVE_QUERY = "fmi::observations::wave::multipointcoverage"
WAVE_PARAMS = "WaveHs,TWATER"
SEA_QUERY = "fmi::observations::mareograph::multipointcoverage"
SEA_PARAMS = "TW_PT1H_AVG"
LATEST_WINDOW = 60 * 60  # some stations only report hourly
SEA_WINDOW = 3 * 3600
HISTORY_WINDOW = 24 * 3600
MAX_AGE = 600
HISTORY_MAX_AGE = 3600
RETRY = 120
TIMEOUT = 15
MAX_BYTES = 4 * 1024 * 1024
MERGE_M = 3000  # a buoy or mareograph this close to a weather station is shown on its marker
MISSING = -1  # in the history arrays (wind is never negative)

Fetch = Callable[[dict[str, Any]], Awaitable[str]]


def _attr(el: ET.Element, name: str) -> str:
    """An attribute by its local name (gml:id, xlink:href), whatever the namespace."""
    for key, value in el.attrib.items():
        if _local(key) == name:
            return value
    return ""


def _float(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return None if math.isnan(value) else value


def parse_multipoint(text: str, expected: str = "") -> tuple[dict[int, tuple[str, float, float]],
                                                           dict[int, list[tuple[int, list[float | None]]]],
                                                           list[str]]:
    """An FMI multipointcoverage answer as (sites, series, fields).

    sites: {fmisid: (name, lat, lon)} from the location list; series: {fmisid: [(unix time,
    [value per field, None when NaN]), ...]}; fields: the parameter names (lower case).
    The coverage lists "lat lon time" rows in one block and the values in another, row for
    row; a row belongs to the station whose point has its coordinates.
    """
    root = ET.fromstring(text)
    sites: dict[int, tuple[str, float, float]] = {}
    refs: dict[str, int] = {}  # point id -> fmisid
    points: dict[str, tuple[float, float]] = {}
    coverages = []
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "Location":
            fmisid, name, ref = None, "", ""
            for child in el:
                kind, space = _local(child.tag), child.get("codeSpace", "")
                if kind == "identifier" and space.endswith("fmisid"):
                    try:
                        fmisid = int((child.text or "").strip())
                    except ValueError:
                        fmisid = None
                elif kind == "name" and space.endswith("/name"):
                    name = (child.text or "").strip()
                elif kind == "representativePoint":
                    ref = _attr(child, "href").lstrip("#")
            if fmisid is not None:
                sites[fmisid] = (name or f"Station {fmisid}", math.nan, math.nan)
                if ref:
                    refs[ref] = fmisid
        elif tag == "Point":
            pos = next((c.text for c in el if _local(c.tag) == "pos"), None)
            parts = (pos or "").split()
            if len(parts) >= 2:
                points[_attr(el, "id")] = (float(parts[0]), float(parts[1]))
        elif tag == "MultiPointCoverage":
            coverages.append(el)

    by_place: dict[tuple[float, float], int] = {}
    for ref, fmisid in refs.items():
        if ref in points:
            lat, lon = points[ref]
            sites[fmisid] = (sites[fmisid][0], lat, lon)
            by_place[(round(lat, 4), round(lon, 4))] = fmisid
    sites = {k: v for k, v in sites.items() if not math.isnan(v[1])}

    series: dict[int, list[tuple[int, list[float | None]]]] = {}
    fields: list[str] = []
    for cov in coverages:
        names, rows = _coverage_rows(cov, expected)
        if names:
            fields = names
        for lat, lon, ts, values in rows:
            fmisid = by_place.get((round(lat, 4), round(lon, 4)))
            if fmisid is not None:
                series.setdefault(fmisid, []).append((ts, values))
    return sites, series, fields


def _coverage_rows(cov: ET.Element, expected: str = "") -> tuple[list[str], list[tuple[float, float, int, list[float | None]]]]:
    """One MultiPointCoverage as (fields, [(lat, lon, unix time, [value per field]), ...]):
    its "lat lon time" positions and its values, row for row."""
    positions = values = ""
    names = []
    for el in cov.iter():
        tag = _local(el.tag)
        if tag == "positions":
            positions = el.text or ""
        elif tag == "doubleOrNilReasonTupleList":
            values = el.text or ""
        elif tag == "field":
            names.append(el.get("name", "").lower())
    if not names:
        names = [p.strip().lower() for p in expected.split(",") if p.strip()]
    if not names:
        return [], []
    pos, vals = positions.split(), values.split()
    width = len(names)
    rows = []
    for row in range(min(len(pos) // 3, len(vals) // width)):
        lat, lon, ts = pos[3 * row: 3 * row + 3]
        rows.append((float(lat), float(lon), int(float(ts)), [_float(v) for v in vals[width * row: width * row + width]]))
    return names, rows


def parse_coverage(text: str, expected: str = "") -> tuple[list[str], list[tuple[float, float, int, list[float | None]]]]:
    """The values of an FMI multipointcoverage answer without its station list, for point
    forecasts asked for by coordinates: (fields, [(lat, lon, unix time, [value per
    field]), ...]), every coverage in it."""
    fields: list[str] = []
    rows: list[tuple[float, float, int, list[float | None]]] = []
    for el in ET.fromstring(text).iter():
        if _local(el.tag) == "MultiPointCoverage":
            names, more = _coverage_rows(el, expected)
            fields = names or fields
            rows.extend(more)
    return fields, rows


def latest_values(fields: list[str], rows: list[tuple[int, list[float | None]]]) -> dict[str, tuple[float, int]]:
    """{field: (value, time)}: the latest value of each field that has one."""
    out: dict[str, tuple[float, int]] = {}
    for ts, values in rows:
        for name, value in zip(fields, values):
            if value is not None and (name not in out or ts >= out[name][1]):
                out[name] = (value, ts)
    return out


def _wind_arrays(fields: list[str], rows: list[tuple[int, list[float | None]]]) -> tuple[array, array, array]:
    """(times, wind ×10, gust ×10) as compact arrays, oldest first."""
    iw = fields.index("windspeedms") if "windspeedms" in fields else None
    ig = fields.index("windgust") if "windgust" in fields else None
    ts, ws, wg = array("i"), array("h"), array("h")
    for t, values in sorted(rows, key=lambda r: r[0]):
        w = values[iw] if iw is not None else None
        g = values[ig] if ig is not None else None
        if w is None and g is None:
            continue
        ts.append(t)
        ws.append(MISSING if w is None else round(w * 10))
        wg.append(MISSING if g is None else round(g * 10))
    return ts, ws, wg


def build_stations(weather: tuple, wave: tuple | None = None, sea: tuple | None = None) -> dict[int, dict[str, Any]]:
    """Stations for the map from parsed answers (see parse_multipoint): the weather stations
    near the sea, with sea temperature and waves from a buoy or mareograph next to one, or
    as their own station when none is."""
    sites, series, fields = weather
    stations: dict[int, dict[str, Any]] = {}
    for fmisid, rows in series.items():
        if fmisid not in sites:
            continue
        name, lat, lon = sites[fmisid]
        if not near_sea(lat, lon):
            continue
        v = latest_values(fields, rows)
        if not v:
            continue
        st: dict[str, Any] = {"id": fmisid, "name": name, "lat": round(lat, 5), "lon": round(lon, 5),
                              "ts": max(t for _, t in v.values())}
        for key, field in (("ws", "windspeedms"), ("wg", "windgust"), ("wd", "winddirection"), ("t", "temperature")):
            if field in v:
                st[key] = round(v[field][0], 1)
        if "ws" in st:
            st["wind_ts"] = v["windspeedms"][1]
        stations[fmisid] = st

    extras: dict[int, dict[str, Any]] = {}
    for parsed, keys in ((wave, (("wave", "wavehs"), ("sea", "twater"))), (sea, (("sea", "tw_pt1h_avg"),))):
        if not parsed:
            continue
        s_sites, s_series, s_fields = parsed
        for fmisid, rows in s_series.items():
            if fmisid not in s_sites:
                continue
            name, lat, lon = s_sites[fmisid]
            if not near_sea(lat, lon):
                continue
            v = latest_values(s_fields, rows)
            entry = extras.setdefault(fmisid, {"id": fmisid, "name": name, "lat": round(lat, 5), "lon": round(lon, 5)})
            for key, field in keys:
                if field in v and key not in entry:
                    entry[key], entry[f"{key}_ts"] = round(v[field][0], 1), v[field][1]
    windy = [s for s in stations.values() if "ws" in s]
    for fmisid, entry in extras.items():
        if "sea" not in entry and "wave" not in entry:
            continue
        near = min(windy, default=None, key=lambda s: distance_m(s["lat"], s["lon"], entry["lat"], entry["lon"]))
        if near and distance_m(near["lat"], near["lon"], entry["lat"], entry["lon"]) <= MERGE_M:
            for key in ("sea", "wave"):
                if key in entry and key not in near:
                    near[key], near[f"{key}_ts"] = entry[key], entry[f"{key}_ts"]
            near["sea_from"] = entry["name"]
        elif fmisid not in stations:
            entry["ts"] = max(entry.get("sea_ts", 0), entry.get("wave_ts", 0))
            stations[fmisid] = entry
    return stations


class Stations:
    def __init__(self, session: aiohttp.ClientSession | None, fetch: Fetch | None = None):
        self.session = session
        self.fetch = fetch or self._get
        self.stations: dict[int, dict[str, Any]] = {}
        self.fetched = 0.0
        self.error: str | None = None
        self._recent: dict[int, tuple[array, array, array]] = {}  # the latest hour, 10-minute steps
        self._history: dict[int, tuple[array, array, array]] = {}  # 24 h, hourly
        self.history_fetched = 0.0
        self._lock = asyncio.Lock()
        self._history_lock = asyncio.Lock()

    async def _get(self, params: dict[str, Any]) -> str:
        async with self.session.get(WFS, params=params, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            resp.raise_for_status()
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("answer too big")
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    @staticmethod
    def _params(query: str, parameters: str, since: float, timestep: int) -> dict[str, Any]:
        return {"service": "WFS", "version": "2.0.0", "request": "getFeature", "storedquery_id": query,
                "bbox": BBOX, "parameters": parameters, "timestep": timestep,
                "starttime": time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime(since))}

    async def latest(self) -> list[dict[str, Any]]:
        """All stations' latest values, refreshed first when older than 10 minutes."""
        async with self._lock:
            if time.time() - self.fetched >= MAX_AGE:
                await self._refresh()
        return list(self.stations.values())

    async def _refresh(self) -> None:
        now = time.time()
        results = await asyncio.gather(
            self.fetch(self._params(WEATHER_QUERY, WEATHER_PARAMS, now - LATEST_WINDOW, 10)),
            self.fetch(self._params(WAVE_QUERY, WAVE_PARAMS, now - SEA_WINDOW, 60)),
            self.fetch(self._params(SEA_QUERY, SEA_PARAMS, now - SEA_WINDOW, 60)),
            return_exceptions=True)
        parsed = []
        for result, expected in zip(results, (WEATHER_PARAMS, WAVE_PARAMS, SEA_PARAMS)):
            if isinstance(result, BaseException):
                if not isinstance(result, (aiohttp.ClientError, asyncio.TimeoutError, ValueError)):
                    raise result
                parsed.append(result)
                continue
            try:
                parsed.append(parse_multipoint(result, expected))
            except (ET.ParseError, ValueError) as err:
                parsed.append(err)
        del results
        weather = parsed[0]
        if isinstance(weather, BaseException):
            if self.error is None:
                _LOGGER.warning("Could not read the weather stations from FMI: %s", weather)
            self.error = str(weather) or type(weather).__name__
            self.fetched = now - MAX_AGE + RETRY
            return
        if self.error:
            _LOGGER.info("Weather stations from FMI are back")
        self.error = None
        sea = [p if not isinstance(p, BaseException) else None for p in parsed[1:]]
        self.stations = build_stations(weather, *sea)
        sites, series, fields = weather
        self._recent = {k: _wind_arrays(fields, rows) for k, rows in series.items() if k in self.stations}
        self.fetched = now

    async def history(self, fmisid: int) -> list[list[Any]] | None:
        """[[time, wind, gust], ...] over the past 24 hours (hourly, then the latest
        10-minute values), or None for an unknown station."""
        if fmisid not in self.stations:
            return None
        async with self._history_lock:
            now = time.time()
            if now - self.history_fetched >= HISTORY_MAX_AGE:
                try:
                    text = await self.fetch(self._params(WEATHER_QUERY, HISTORY_PARAMS, now - HISTORY_WINDOW, 60))
                    sites, series, fields = await asyncio.to_thread(parse_multipoint, text, HISTORY_PARAMS)
                    del text
                    self._history = {k: _wind_arrays(fields, rows) for k, rows in series.items()
                                     if k in self.stations}
                    self.history_fetched = now
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, ET.ParseError) as err:
                    _LOGGER.debug("Could not read the station history from FMI: %s", err)
                    self.history_fetched = now - HISTORY_MAX_AGE + RETRY
        hourly = self._history.get(fmisid, (array("i"), array("h"), array("h")))
        recent = self._recent.get(fmisid, (array("i"), array("h"), array("h")))
        start = recent[0][0] if len(recent[0]) else math.inf  # the 10-minute values cover from here
        cutoff = time.time() - HISTORY_WINDOW - 3600
        rows = [r for r in zip(*hourly) if cutoff <= r[0] < start] + list(zip(*recent))
        return [[ts, None if ws == MISSING else ws / 10, None if wg == MISSING else wg / 10] for ts, ws, wg in rows]

    def memory_bytes(self) -> int:
        """Roughly what the history arrays take, for the tests."""
        return sum(sum(a.itemsize * len(a) for a in arrs) for arrs in (*self._history.values(), *self._recent.values()))
