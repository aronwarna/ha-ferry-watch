"""Sea ice on the Porkkala inner route, read once a day from December to April.

Two open sources, neither needs a key:

- FMI's manual sea ice observations (open data stored query
  fmi::observations::seaice::manual::timevaluepair) at Kirkkonummi Porkkala (fmisid
  103933, 59.95 N 24.33 E, a few km from the inner route): the total ice thickness
  (ICE_PT1S_INSTANT, cm) and the snow on the ice (SNDICE_PT1M_AVG, cm), measured by an
  observer about once a week while there is ice. No reports at all means no ice.
- FMI's daily Baltic ice chart, as the Copernicus Marine Service publishes it
  (SEAICE_BAL_SEAICE_L4_NRT_OBSERVATIONS_011_004, ice concentration on a 1 km grid),
  read for one point on the route with the open WMTS GetFeatureInfo (no account).

Each winter day gets one row in the ice_days table; passages copy the day's state. Each
source's failure is logged once, and the other one still counts.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

import aiohttp

from .config import USER_AGENT
from .store import Store

_LOGGER = logging.getLogger(__name__)

WFS = "https://opendata.fmi.fi/wfs"
OBS_QUERY = "fmi::observations::seaice::manual::timevaluepair"
OBS_STATION = 103933  # Kirkkonummi Porkkala
OBS_DAYS = 30  # observations looked at
OBS_FRESH = 12 * 86400  # a thickness older than this no longer says there is ice now
CHART_URL = "https://wmts.marine.copernicus.eu/teroWmts"
CHART_LAYER = "SEAICE_BAL_SEAICE_L4_NRT_OBSERVATIONS_011_004/FMI-BAL-SEAICE_CONC-L4-NRT-OBS/ice_concentration"
ROUTE_POINT = (59.97, 24.40)  # on the inner route off Porkkala
CHART_ZOOM = 10  # tile level for GetFeatureInfo: about 150 m pixels on the 1 km chart
ICE_FROM = 10  # % concentration that counts as ice (below is open water on ice charts)
WINTER_MONTHS = {12, 1, 2, 3, 4}
RETRY = 3600
TIMEOUT = 15
CREDIT = "FMI sea ice observations (CC BY 4.0); FMI ice chart via E.U. Copernicus Marine Service"

Fetch = Callable[[str, dict[str, Any]], Awaitable[Any]]


def is_winter(ts: float) -> bool:
    return time.localtime(ts).tm_mon in WINTER_MONTHS


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_timevaluepair(text: str) -> dict[str, list[tuple[float, float]]]:
    """{parameter: [(UNIX time, value), ...]} from an FMI timevaluepair answer: one
    wfs:member per station and parameter, the name in its observedProperty link
    (…&param=ICE_PT1S_INSTANT&…) and the values as wml2:MeasurementTVP. NaN is skipped."""
    out: dict[str, list[tuple[float, float]]] = {}
    for member in ET.fromstring(text):
        if _local(member.tag) != "member":
            continue
        name, pairs = None, []
        for el in member.iter():
            tag = _local(el.tag)
            if tag == "observedProperty" and not name:
                href = next((v for k, v in el.attrib.items() if _local(k) == "href"), "")
                name = (parse_qs(urlsplit(href).query).get("param") or [None])[0]
            elif tag == "MeasurementTimeseries" and not name:
                ident = next((v for k, v in el.attrib.items() if _local(k) == "id"), "")
                name = ident.rsplit("-", 1)[-1] or None
            elif tag == "MeasurementTVP":
                fields = {_local(c.tag): (c.text or "").strip() for c in el}
                try:
                    value = float(fields["value"])
                    ts = datetime.fromisoformat(fields["time"].replace("Z", "+00:00")).timestamp()
                except (KeyError, ValueError):
                    continue
                if not math.isnan(value):
                    pairs.append((ts, value))
        if name and pairs:
            out.setdefault(name, []).extend(pairs)
    for pairs in out.values():
        pairs.sort()
    return out


def latest_thickness(values: dict[str, list[tuple[float, float]]]) -> dict[str, Any] | None:
    thick = values.get("ICE_PT1S_INSTANT") or []
    if not thick:
        return None
    ts, cm = thick[-1]
    snow = dict(values.get("SNDICE_PT1M_AVG") or []).get(ts)
    return {"ts": int(ts), "cm": cm, "snow_cm": snow}


def chart_tile(lat: float, lon: float, zoom: int = CHART_ZOOM) -> tuple[int, int, int, int]:
    """(tile column, tile row, i, j) of a point in the EPSG:3857 tile grid."""
    n = 2 ** zoom
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return int(x), int(y), int((x % 1) * 256), int((y % 1) * 256)


def parse_chart(data: Any) -> float | None:
    """The ice concentration (%) in a GetFeatureInfo JSON answer. The service's exact
    layout isn't documented, so take the first number under a key that names the value
    (value, ice_concentration, …), searching features/properties at any depth; None
    for no data (land, outside the chart, or no chart that day)."""
    keys = ("ice_concentration", "value", "val", "values", "data")

    def number(v: Any) -> float | None:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return None if math.isnan(v) else float(v)
        if isinstance(v, str):
            try:
                f = float(v)
            except ValueError:
                return None
            return None if math.isnan(f) else f
        if isinstance(v, list) and v:
            return number(v[0])
        return None

    def walk(obj: Any, depth: int = 0) -> float | None:
        if depth > 6:
            return None
        if isinstance(obj, dict):
            for key in keys:
                if key in obj and (found := number(obj[key])) is not None:
                    return found
            for v in obj.values():
                if isinstance(v, (dict, list)) and (found := walk(v, depth + 1)) is not None:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                if (found := walk(v, depth + 1)) is not None:
                    return found
        return None

    found = walk(data)
    return None if found is None or not 0 <= found <= 100 else round(found, 1)


def ice_state(conc: float | None, thick: dict[str, Any] | None, obs_ok: bool, now: float) -> str | None:
    """'ice', 'open' or None (unknown). The chart speaks for the route itself; without it
    the latest thickness at Porkkala decides, and no report for a month means no ice."""
    if conc is not None:
        return "ice" if conc >= ICE_FROM else "open"
    if thick and now - thick["ts"] <= OBS_FRESH:
        return "ice" if thick["cm"] > 0 else "open"
    if obs_ok and (thick is None or thick["cm"] == 0):
        return "open"
    return None


class IceWatch:
    def __init__(self, session: aiohttp.ClientSession | None, store: Store, fetch: Fetch | None = None):
        self.session = session
        self.store = store
        self.fetch = fetch or self._get
        self.errors: dict[str, str | None] = {"obs": None, "chart": None}
        self._task: asyncio.Task | None = None
        self._tried = 0.0

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        async with self.session.get(url, params=params, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            resp.raise_for_status()
            text = await resp.text()
        return text

    async def _observations(self, now: float) -> dict[str, Any] | None:
        start = time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime(now - OBS_DAYS * 86400))
        text = await self.fetch(WFS, {"service": "WFS", "version": "2.0.0", "request": "getFeature",
                                      "storedquery_id": OBS_QUERY, "fmisid": OBS_STATION, "starttime": start})
        return latest_thickness(parse_timevaluepair(text))

    async def _chart(self) -> float | None:
        import json
        col, row, i, j = chart_tile(*ROUTE_POINT)
        text = await self.fetch(CHART_URL, {"service": "WMTS", "request": "GetFeatureInfo", "layer": CHART_LAYER,
                                            "tilematrixset": "EPSG:3857", "tilematrix": CHART_ZOOM, "tilerow": row,
                                            "tilecol": col, "i": i, "j": j, "INFOFORMAT": "application/json"})
        return parse_chart(json.loads(text) if isinstance(text, str) else text)

    def _failed(self, source: str, err: BaseException) -> None:
        if self.errors[source] is None:
            name = "ice observations from FMI" if source == "obs" else "the ice chart from Copernicus Marine"
            _LOGGER.warning("Could not read %s: %s", name, err)
        self.errors[source] = str(err) or type(err).__name__

    async def check(self, now: float | None = None) -> dict[str, Any]:
        """Read both sources now and store today's row."""
        now = now or time.time()
        self._tried = now
        obs, chart = await asyncio.gather(self._observations(now), self._chart(), return_exceptions=True)
        expected = (aiohttp.ClientError, asyncio.TimeoutError, ValueError, ET.ParseError, TypeError)
        for source, result in (("obs", obs), ("chart", chart)):
            if isinstance(result, BaseException):
                if not isinstance(result, expected):
                    raise result
                self._failed(source, result)
            elif self.errors[source]:
                _LOGGER.info("Ice data is back (%s)", source)
                self.errors[source] = None
        thick = None if isinstance(obs, BaseException) else obs
        conc = None if isinstance(chart, BaseException) else chart
        row = {"day": day_of(now), "checked_ts": int(now),
               "state": ice_state(conc, thick, not isinstance(obs, BaseException), now),
               "conc": conc, "thick_cm": thick["cm"] if thick else None,
               "thick_ts": thick["ts"] if thick else None, "snow_cm": thick["snow_cm"] if thick else None}
        self.store.set_ice_day(row)
        return row

    async def current(self, now: float | None = None) -> dict[str, Any] | None:
        """Today's ice, read first if it hasn't been today (a failed read is retried after
        an hour); None outside the winter months."""
        now = now or time.time()
        if not is_winter(now):
            return None
        row = self.store.ice_day(day_of(now))
        if row and (row["state"] is not None or now - self._tried < RETRY):
            return row
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self.check(now))
        return await asyncio.shield(self._task)

    async def run(self) -> None:
        """Once a day in winter, so there is a row for every day even without passages."""
        while True:
            try:
                await self.current()
            except Exception:  # noqa: BLE001 - keep the loop alive
                _LOGGER.exception("Ice check failed")
            await asyncio.sleep(RETRY)
