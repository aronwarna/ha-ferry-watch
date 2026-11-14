"""Blue-green algae over the sea, from the Finnish Environment Institute's (SYKE) open data.

Two layers:

- "algae": SYKE's daily surface algae interpretation from Sentinel-3 OLCI (the map behind
  TARKKA and vesi.fi's algae view), 300 m pixels over the whole Baltic, in four classes:
  1 no surface algae, 2 possible, 3 probable, 4 certain; nothing where it was cloudy or no
  satellite passed. A WMS (geoserver2.ymparisto.fi, layer eo:EO_MR_OLCI_ALGAE) with a daily
  time dimension, read from the layer's own small GetCapabilities. The panel recolours the
  classes and cuts them to the drawn chart's sea (fields.js waterMask, as the sea layers).
  The tap value comes from GetFeatureInfo ({"algae_index": n}).
- "algae-reports": the algae observations people send to SYKE (the national algae
  monitoring's weekly sites, beach keepers and anyone with vesi.fi or Järviwiki), from
  SYKE's Open311 API: each a place, a time and a level 0 none, 1 some, 2 plenty, 3 very
  much, with an optional note. Only those at sea or on the shore (within SHORE_M of
  Natural Earth's sea) are kept; lakes are left out. Each day on the time bar shows the
  reports of that day and the WINDOW_DAYS - 1 before, the older ones fainter.

Blooms come in summer. Out of season (October to May) both layers show the last season,
so it can still be looked at (and the reports aren't fetched again for a week).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from .config import USER_AGENT
from .overlays import Overlay, Overlays, RunBuilder, parse_time_dimension

_LOGGER = logging.getLogger(__name__)

ID = "algae"
REPORTS_ID = "algae-reports"
TIMEOUT = 30
MAX_BYTES = 8 * 1024 * 1024
DAY = 86400

# --- the satellite layer ----------------------------------------------------------------
WMS = "https://geoserver2.ymparisto.fi/geoserver/eo/wms"
LAYER = "EO_MR_OLCI_ALGAE"
CAPS_URLS = (f"https://geoserver2.ymparisto.fi/geoserver/eo/{LAYER}/wms", WMS)
TILE_URL = (WMS + "?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=" + LAYER + "&STYLES="
            + "&CRS=EPSG:3857&BBOX={bbox}&WIDTH=256&HEIGHT=256&FORMAT=image/png&TRANSPARENT=TRUE&TIME={date}")
# SYKE's legend (GetLegendGraphic): 1 "Ei" #2B7EA8, 2 "Mahdollista" #FFFFC9,
# 3 "Todennäköistä" #FFDC28, 4 "Varmaa" #CD3301.
CLASSES = {1: "No surface algae seen", 2: "Surface algae possible", 3: "Surface algae probable",
           4: "Surface algae certain"}
BOUNDS = (54.3, 13.5, 63.3, 28.9)  # the layer's box (south, west, north, east)
SEASON_START = (5, 1)  # month, day: the season's first day kept on the bar
IN_SEASON_DAYS = 45  # in season, the bar goes back this many days
MAX_DAYS = 160
ATTRIBUTION = "Algae: Finnish Environment Institute SYKE (CC BY 4.0)"

_NAME = re.compile(r"<Name>\s*(?:eo:)?" + LAYER + r"\s*</Name>")
_TIME = re.compile(r"<Dimension\b[^>]*\bname=\"time\"[^>]*>([^<]*)</Dimension>", re.I)


def in_season(ts: float) -> bool:
    """June to September: blooms are reported and the satellite looks."""
    return 6 <= time.gmtime(ts).tm_mon <= 9


def season_window(now: float) -> tuple[int, int]:
    """The days the layers show: in season the last IN_SEASON_DAYS (from 1 May at the
    earliest); out of season the whole last season, June to September."""
    t = time.gmtime(now)
    today = int(now // DAY * DAY)
    if 5 <= t.tm_mon <= 9:
        start = _utc(t.tm_year, *SEASON_START)
        return max(start, today - (IN_SEASON_DAYS - 1) * DAY), today
    year = t.tm_year if t.tm_mon >= 10 else t.tm_year - 1
    return _utc(year, 6, 1), _utc(year, 9, 30)


def _utc(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def days_from_capabilities(xml: str) -> list[int]:
    """The layer's days (00:00 UTC each), from its WMS 1.3.0 capabilities."""
    m = _NAME.search(xml)
    if not m:
        return []
    end = len(xml)
    for stop in ("<Layer", "</Layer>"):
        i = xml.find(stop, m.end())
        if 0 <= i < end:
            end = i
    dim = _TIME.search(xml[m.end():end])
    if not dim or not dim.group(1).strip():
        return []
    return sorted({t // DAY * DAY for t in parse_time_dimension(dim.group(1), limit=4000)})


def pick_days(days: list[int], now: float) -> list[int]:
    """The listed days in the season window; with none there (the satellite's season ended
    early, or a summer's first days), the newest season the list has."""
    start, end = season_window(now)
    picked = [d for d in days if start <= d <= end]
    if not picked and days:
        newest = days[-1]
        year = time.gmtime(newest).tm_year
        picked = [d for d in days if _utc(year, *SEASON_START) <= d <= newest]
    return picked[-MAX_DAYS:]


async def _get_text(session: aiohttp.ClientSession, url: str, params: Any) -> str:
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


Getter = Callable[[str, Any], Awaitable[str]]


def _getter(session: aiohttp.ClientSession | None, fetch: Getter | None) -> Getter:
    if fetch:
        return fetch

    async def get(url: str, params: Any) -> str:
        if session is None:
            raise ValueError("no connection")
        return await _get_text(session, url, params)
    return get


def satellite(fetch: Getter | None = None, **changes: Any) -> Overlay:
    async def list_times(session: aiohttp.ClientSession | None, now: float) -> list[float]:
        get = _getter(session, fetch)
        params = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetCapabilities"}
        err: Exception | None = None
        for url in CAPS_URLS:
            try:
                days = days_from_capabilities(await get(url, params))
            except Exception as e:  # noqa: BLE001 - the next address
                err = e
                continue
            if days:
                return [d for d in pick_days(days, now) if d <= now]
        raise ValueError(f"no days in SYKE's capabilities ({err or 'layer not listed'})")

    async def value(lat: float, lon: float, ts: int) -> dict[str, Any] | None:
        get = _getter(o_session[0], fetch)
        d = 0.01
        params = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetFeatureInfo", "LAYERS": LAYER,
                  "QUERY_LAYERS": LAYER, "STYLES": "", "CRS": "EPSG:4326",
                  "BBOX": f"{lat - d},{lon - d},{lat + d},{lon + d}", "WIDTH": "101", "HEIGHT": "101",
                  "I": "50", "J": "50", "INFO_FORMAT": "application/json",
                  "TIME": time.strftime("%Y-%m-%d", time.gmtime(ts))}
        try:
            data = json.loads(await get(WMS, params))
        except Exception as err:  # noqa: BLE001 - no value, said so
            _LOGGER.debug("Algae value failed: %s", err)
            return {"index": None, "error": str(err) or type(err).__name__}
        index = feature_index(data)
        if index is None and not await asyncio.to_thread(at_sea, lat, lon, 0):
            return None  # the land: nothing to say
        return {"index": index, "date": params["TIME"]}

    o_session: list[aiohttp.ClientSession | None] = [None]
    kw: dict[str, Any] = dict(
        id=ID, title="Blue-green algae (satellite)", kind="tiles", url=TILE_URL, times=list_times,
        max_age=30 * DAY, refresh=3 * 3600, frame_ms=700, opacity=0.85, color="#84cc16", icon="algae",
        min_zoom=4, max_zoom=10, bounds=BOUNDS, valid_for=DAY, value=value, sea=True, attribution=ATTRIBUTION,
        note="SYKE's daily satellite map of surface algae (Sentinel-3, 300 m): yellow possible, orange "
             "probable, red certain; faint blue where the sea was seen clear. Nothing under clouds. "
             "Out of season, last summer.",
    )
    kw.update(changes)
    o = Overlay(**kw)
    o._session_box = o_session  # type: ignore[attr-defined]  # set at register
    return o


def feature_index(data: Any) -> int | None:
    """The class in a GetFeatureInfo answer: GeoServer's GeoJSON ({"features": [{"properties":
    {"algae_index": n}}]}) or a plain {"algae_index": n}; 0 or none where it was cloudy."""
    props: Any = data
    if isinstance(data, dict) and data.get("features"):
        props = data["features"][0].get("properties") or {}
    if not isinstance(props, dict):
        return None
    for k, v in props.items():
        if "algae" in k.lower() or k.upper() in ("GRAY_INDEX", "VALUE", "BAND1"):
            try:
                n = int(round(float(v)))
            except (TypeError, ValueError):
                return None
            return n if n in CLASSES else None
    return None


# --- the observation reports -----------------------------------------------------------
OPEN311 = "https://rajapinnat.ymparisto.fi/api/kansalaishavainnot/1.0/requests.json"
SERVICE = "algaebloom_service_code_201808151546171"
LEVEL_KEY = "Ecology_algaebloom_singlevaluelist_201808151546174"
CAP = 1000  # records an answer holds at most: a chunk that hits it is split
CHUNK_DAYS = 7
WINDOW_DAYS = 3  # a day on the bar shows its reports and those of the two days before
SHORE_M = 2000  # reports this close to Natural Earth's sea count as the sea's
LEVELS = {0: "No algae", 1: "Some algae", 2: "Plenty of algae", 3: "Very much algae"}
KINDS = {0: "Report", 1: "Experienced observer", 2: "National algae monitoring site"}
REPORTS_ATTRIBUTION = "Algae reports: SYKE, vesi.fi and Järviwiki (CC BY 4.0)"

_LEVEL = re.compile(re.escape(LEVEL_KEY) + r":(\d)")
_NOTE = re.compile(r"Ecology_additionalinfo:(.*)$", re.S)
_SITE = re.compile(r"curid=(\d+)|#_jwSiteID_(\d+)")

try:
    from zoneinfo import ZoneInfo
    _HELSINKI: Any = ZoneInfo("Europe/Helsinki")
except Exception:  # noqa: BLE001 - no tz database: summer time, as in the season
    _HELSINKI = timezone(timedelta(hours=3))


def parse_report(r: dict[str, Any]) -> dict[str, Any] | None:
    """One Open311 record as {lat, lon, ts, level, kind, note, site}, None if unusable."""
    try:
        lat, lon = float(r["lat"]), float(r["long"])
    except (KeyError, TypeError, ValueError):
        return None
    desc = str(r.get("description") or "")
    m = _LEVEL.search(desc)
    if not m or int(m.group(1)) not in LEVELS:
        return None
    try:
        # Local Finnish time without a zone ("2025-07-20T23:41:00.0000000").
        text = str(r["requested_datetime"])[:19]
        t = datetime.fromisoformat(text)
        t = t.replace(tzinfo=_HELSINKI) if t.tzinfo is None else t
        ts = int(t.timestamp())
    except (KeyError, ValueError):
        return None
    kind = 2 if "#_jwMonitoring_" in desc else 1 if re.search(r"#_jwMaintainer_(expert|experienced)", desc) else 0
    note = ""
    n = _NOTE.search(desc)
    if n:
        note = n.group(1).split("#_")[0].strip(" ,;-\n")
    site = _SITE.search(desc)
    return {"lat": round(lat, 5), "lon": round(lon, 5), "ts": ts, "level": int(m.group(1)), "kind": kind,
            "note": note[:200], "site": int(site.group(1) or site.group(2)) if site else None}


def at_sea(lat: float, lon: float, radius: float = SHORE_M) -> bool:
    """On Natural Earth's sea, or within `radius` metres of it (a beach, a jetty)."""
    from .vchart import lonlat_to_mercator, ne_base
    x, y = lonlat_to_mercator(lon, lat)
    r = radius / math.cos(math.radians(lat))  # metres on the ground to mercator metres
    probes = [(x, y)] + [(x + r * math.cos(a), y + r * math.sin(a)) for a in
                         (i * math.pi / 4 for i in range(8))] + [(x + r / 2 * math.cos(a), y + r / 2 * math.sin(a))
                                                                  for a in (i * math.pi / 2 for i in range(4))]
    polys = [p for p in ne_base()["sea"] if p[0][0] - r <= x <= p[0][2] + r and p[0][1] - r <= y <= p[0][3] + r]
    for px, py in probes:
        for (minx, miny, maxx, maxy), rings in polys:
            if minx <= px <= maxx and miny <= py <= maxy and _in_rings(px, py, rings):
                return True
    return False


def _in_rings(x: float, y: float, rings: list) -> bool:
    inside = False
    for ring in rings:
        n = len(ring) // 2
        j = n - 1
        for i in range(n):
            xi, yi, xj, yj = ring[2 * i], ring[2 * i + 1], ring[2 * j], ring[2 * j + 1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


async def fetch_reports(get_json: Callable[[str, Any], Awaitable[Any]], start: int, end: int) -> list[dict[str, Any]]:
    """Every report from `start` to `end` (s), a week at a time; a week that hits the API's
    cap is asked for again in halves."""
    out: dict[Any, dict[str, Any]] = {}

    async def span(a: int, b: int) -> None:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        params = {"service_code": SERVICE, "start_date": time.strftime(fmt, time.gmtime(a)),
                  "end_date": time.strftime(fmt, time.gmtime(b))}
        rows = await get_json(OPEN311, params)
        if not isinstance(rows, list):
            raise ValueError("reports: not a list")
        if len(rows) >= CAP and b - a > 3600:
            mid = (a + b) // 2
            await span(a, mid)
            await span(mid, b)
            return
        for r in rows:
            if isinstance(r, dict):
                out[r.get("service_request_id") or (r.get("lat"), r.get("long"), r.get("requested_datetime"))] = r

    a = start
    while a < end:
        b = min(end, a + CHUNK_DAYS * DAY)
        await span(a, b)
        a = b
    return list(out.values())


def build_reports_run(rows: list[dict[str, Any]], days: list[int], sea: Callable[[float, float], bool] = at_sea
                      ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The binary run (fields level, kind, age in days; a report only on the days whose
    window holds it) and the reports themselves, in the run's point order."""
    reports = [p for p in (parse_report(r) for r in rows) if p]
    lo, hi = (days[0] - (WINDOW_DAYS - 1) * DAY, days[-1] + DAY) if days else (0, 0)
    reports = [p for p in reports if lo <= p["ts"] < hi]
    reports = [p for p in reports if sea(p["lat"], p["lon"])]
    reports.sort(key=lambda p: (p["ts"], p["lat"]))
    b = RunBuilder(days, ["level", "kind", "age"], [(p["lat"], p["lon"]) for p in reports], scale=[1, 1, 10])
    for i, p in enumerate(reports):
        day = p["ts"] // DAY * DAY
        for k in range(WINDOW_DAYS):
            d = day + k * DAY
            if d in b.filled:
                b.set(i, d, 0, p["level"])
                b.set(i, d, 1, p["kind"])
                b.set(i, d, 2, max(0.0, (d + DAY - p["ts"]) / DAY))
    run = {"times": list(days), "fields": b.fields, "points": b.points, "scale": b.scale,
           "values": {str(t): b.values[t] for t in days}}
    return run, reports


class Reports:
    """The reports overlay: its run, and the reports' details (notes) for a tap."""

    def __init__(self, directory: Path, fetch_json: Callable[[str, Any], Awaitable[Any]] | None = None):
        self.path = directory / "points" / f"{REPORTS_ID}-details.json"
        self.fetch_json = fetch_json
        self.details: list[dict[str, Any]] | None = None
        self.overlay: Overlay | None = None

    async def points(self, session: aiohttp.ClientSession | None, now: float) -> dict[str, Any]:
        get = self.fetch_json
        if get is None:
            text = _getter(session, None)

            async def get(url: str, params: Any) -> Any:
                return json.loads(await text(url, params))
        start, end = season_window(now)
        days = list(range(start, min(end, int(now // DAY * DAY)) + 1, DAY))
        rows = await fetch_reports(get, start - (WINDOW_DAYS - 1) * DAY, min(now, end + DAY))
        run, reports = await asyncio.to_thread(build_reports_run, rows, days)
        if self.overlay:  # out of season the last season doesn't change: a week before asking again
            self.overlay.max_age = 3600 if 5 <= time.gmtime(now).tm_mon <= 9 else 7 * DAY
        self.details = reports
        await asyncio.to_thread(_write_json, self.path, reports)
        _LOGGER.debug("Algae reports: %s at sea of %s, %s days", len(reports), len(rows), len(days))
        return run

    async def value(self, lat: float, lon: float, ts: int) -> dict[str, Any] | None:
        """The reports at a place (the panel sends a drawn report's own place), newest first."""
        if self.details is None:
            self.details = await asyncio.to_thread(_read_json, self.path) or []
        day = ts // DAY * DAY
        lo, hi = day - (WINDOW_DAYS - 1) * DAY, day + DAY
        near = [p for p in self.details if abs(p["lat"] - lat) < 2e-4 and abs(p["lon"] - lon) < 4e-4
                and lo <= p["ts"] < hi]
        if not near:
            return None
        near.sort(key=lambda p: -p["ts"])
        return {"reports": [{**p, "level_text": LEVELS[p["level"]], "kind_text": KINDS[p["kind"]]} for p in near[:3]]}


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def register(overlays: Overlays, fetch: Getter | None = None,
             fetch_json: Callable[[str, Any], Awaitable[Any]] | None = None, **changes: Any) -> Reports:
    sat = satellite(fetch, **changes.get("satellite", {}))
    sat._session_box[0] = overlays.session  # type: ignore[attr-defined]
    overlays.register(sat)
    reports = Reports(overlays.dir, fetch_json)
    o = Overlay(
        id=REPORTS_ID, title="Blue-green algae (reports)", kind="points", points=reports.points,
        value=reports.value, max_age=3600, refresh=3600, color="#16a34a", icon="algae", group="top",
        opacity=1.0, min_zoom=4, bounds=(59.0, 19.0, 66.0, 31.0), valid_for=DAY, frame_ms=700,
        sparse=True, attribution=REPORTS_ATTRIBUTION,
        note="Algae seen on the shore and at sea: SYKE's monitoring sites and people's reports, the "
             "last three days. Green none, yellow some, orange plenty, red very much. Tap one for "
             "the note.",
        **changes.get("reports", {}),
    )
    reports.overlay = o
    overlays.register(o)
    return reports
