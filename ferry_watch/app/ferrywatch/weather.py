"""Sea conditions near Porkkala from the Finnish Meteorological Institute's open data
(CC BY 4.0, no key needed).

Fetched only while a panel is open (every 10 minutes) or when an alert is going out, as
four small requests; only a dozen numbers are kept, and the wind is stored with each
passage. Every request has a timeout and a failure keeps the previous values.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Awaitable, Callable

import aiohttp

from .config import USER_AGENT
from .geo import compass

_LOGGER = logging.getLogger(__name__)

WFS = "https://opendata.fmi.fi/wfs"
# (key, stored query, FMI station id, parameters, minutes back, station name)
QUERIES = (
    ("porkkala", "fmi::observations::weather::simple", 100997, "ws_10min,wg_10min,wd_10min,t2m", 40,
     "Kirkkonummi Mäkiluoto"),
    ("harmaja", "fmi::observations::weather::simple", 100996, "vis,wawa", 40, "Helsinki Harmaja"),
    ("sea", "fmi::observations::mareograph::simple", 132310, "WATLEV,TW_PT1H_AVG", 90, "Helsinki Kaivopuisto"),
    ("waves", "fmi::observations::wave::simple", 134221, "WaveHs,ModalWDi", 120, "Gulf of Finland wave buoy"),
)
MAX_AGE = 600  # seconds between refreshes while a panel is open
RETRY = 120  # after a failed refresh
FRESH = 3600  # older observations aren't quoted in alerts
KEEP = 6 * 3600  # older ones aren't shown at all (e.g. the wave buoy, taken in for winter)
TIMEOUT = 8
MAX_BYTES = 512 * 1024
CREDIT = "Finnish Meteorological Institute (CC BY 4.0)"

Fetch = Callable[[dict[str, Any]], Awaitable[str]]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_simple(text: str) -> dict[str, tuple[float, float]]:
    """{parameter: (value, UNIX time)}: the latest value of each parameter in an FMI
    'simple' WFS answer (one BsWfsElement per parameter and time; NaN means missing)."""
    out: dict[str, tuple[float, float]] = {}
    for el in ET.fromstring(text).iter():
        if _local(el.tag) != "BsWfsElement":
            continue
        fields = {_local(child.tag): (child.text or "").strip() for child in el}
        try:
            value = float(fields["ParameterValue"])
            ts = datetime.fromisoformat(fields["Time"].replace("Z", "+00:00")).timestamp()
            name = fields["ParameterName"]
        except (KeyError, ValueError):
            continue
        if math.isnan(value) or not name:
            continue
        if name not in out or ts >= out[name][1]:
            out[name] = (value, ts)
    return out


# Present weather (WMO code table 4680, automatic stations), in a word.
def weather_word(code: float | None) -> str | None:
    if code is None:
        return None
    c = int(code)
    for low, high, word in ((10, 10, "mist"), (30, 35, "fog"), (40, 42, "precipitation"), (50, 59, "drizzle"),
                            (60, 69, "rain"), (70, 79, "snow"), (80, 84, "showers"), (85, 87, "snow showers"),
                            (89, 89, "hail"), (90, 99, "thunder")):
        if low <= c <= high:
            return word
    return None


def visibility_text(km: float) -> str:
    if km < 1:
        return f"{round(km * 1000, -2):.0f} m"
    return f"{km:.1f} km" if km < 10 else f"{km:.0f} km"


class SeaWeather:
    def __init__(self, session: aiohttp.ClientSession | None, fetch: Fetch | None = None):
        self.session = session
        self.fetch = fetch or self._get
        self.stations: dict[str, dict[str, tuple[float, float]]] = {}
        self.fetched = 0.0
        self.error: str | None = None
        self._task: asyncio.Task | None = None

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

    async def _station(self, key: str, query: str, fmisid: int, parameters: str, minutes: int) -> None:
        start = time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime(time.time() - minutes * 60))
        text = await self.fetch({"service": "WFS", "version": "2.0.0", "request": "getFeature",
                                 "storedquery_id": query, "fmisid": fmisid, "parameters": parameters,
                                 "starttime": start})
        values = parse_simple(text)
        if values:
            self.stations[key] = {**self.stations.get(key, {}), **values}

    async def refresh(self) -> None:
        results = await asyncio.gather(*(self._station(k, q, s, p, m) for k, q, s, p, m, _ in QUERIES),
                                       return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        for err in errors:
            if not isinstance(err, (aiohttp.ClientError, asyncio.TimeoutError, ValueError, ET.ParseError)):
                raise err
        if len(errors) == len(results):
            error = str(errors[0]) or type(errors[0]).__name__
            if self.error is None:
                _LOGGER.warning("Could not read sea conditions from FMI: %s", error)
            self.error = error
            self.fetched = time.time() - MAX_AGE + RETRY
            return
        if self.error:
            _LOGGER.info("Sea conditions from FMI are back")
        self.error = None
        self.fetched = time.time()

    def start_refresh(self, max_age: float = MAX_AGE) -> asyncio.Task | None:
        """Start a refresh if the values are older than max_age; returns the running one."""
        if self._task and not self._task.done():
            return self._task
        if time.time() - self.fetched <= max_age:
            return None
        self._task = asyncio.create_task(self.refresh())
        return self._task

    async def latest(self, max_age: float = MAX_AGE, wait: float | None = None) -> dict[str, Any]:
        """Current conditions, refreshed first if older than max_age; waits at most `wait`
        seconds for that (then the refresh carries on and the old values are returned)."""
        task = self.start_refresh(max_age)
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), wait)
            except asyncio.TimeoutError:
                pass
        return self.conditions()

    def _value(self, key: str, param: str, now: float) -> tuple[float, float] | None:
        found = self.stations.get(key, {}).get(param)
        return found if found and now - found[1] <= KEEP else None

    def conditions(self, now: float | None = None) -> dict[str, Any]:
        """The values the panel and alerts use, each with its observation time and station."""
        now = now or time.time()
        names = {k: name for k, *_, name in QUERIES}
        out: dict[str, Any] = {"fetched": self.fetched or None, "error": self.error}
        ws = self._value("porkkala", "ws_10min", now)
        if ws:
            gust, wd = self._value("porkkala", "wg_10min", now), self._value("porkkala", "wd_10min", now)
            out["wind"] = {"ms": ws[0], "gust": gust[0] if gust else None, "dir": wd[0] if wd else None,
                           "ts": ws[1], "where": names["porkkala"]}
        if t := self._value("porkkala", "t2m", now):
            out["air"] = {"c": t[0], "ts": t[1], "where": names["porkkala"]}
        if vis := self._value("harmaja", "vis", now):
            wawa = self._value("harmaja", "wawa", now)
            out["visibility"] = {"km": round(vis[0] / 1000, 1), "weather": weather_word(wawa[0] if wawa else None),
                                 "ts": vis[1], "where": names["harmaja"]}
        if level := self._value("sea", "WATLEV", now):
            out["sea_level"] = {"cm": round(level[0] / 10), "ts": level[1], "where": names["sea"]}
        if water := self._value("sea", "TW_PT1H_AVG", now):
            out["water"] = {"c": water[0], "ts": water[1], "where": names["sea"]}
        if waves := self._value("waves", "WaveHs", now):
            wdir = self._value("waves", "ModalWDi", now)
            out["waves"] = {"m": waves[0], "dir": wdir[0] if wdir else None, "ts": waves[1], "where": names["waves"]}
        return out


def alert_parts(cond: dict[str, Any], now: float | None = None) -> list[str]:
    """Fresh wind and visibility as short phrases for the alert text."""
    now = now or time.time()
    parts = []
    wind = cond.get("wind")
    if wind and now - wind["ts"] <= FRESH:
        text = f"Wind {compass(wind['dir'])} {wind['ms']:.0f} m/s".replace("  ", " ")
        if wind.get("gust") and wind["gust"] - wind["ms"] >= 3:
            text += f" (gusts {wind['gust']:.0f})"
        parts.append(text)
    vis = cond.get("visibility")
    if vis and now - vis["ts"] <= FRESH:
        parts.append(f"visibility {visibility_text(vis['km'])}" + (f" ({vis['weather']})" if vis["weather"] else ""))
    return parts
