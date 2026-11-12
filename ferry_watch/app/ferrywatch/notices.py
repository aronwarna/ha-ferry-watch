"""Things a boater is warned about or has to plan round, from open data (all CC BY 4.0):

- nautical warnings (Fintraffic, through Digitraffic): firing exercises, works in a fairway,
  missing or moved marks, drifting objects, with their place as a point, line or area;
- marine weather warnings (the Finnish Meteorological Institute's CAP feed): wind, wave
  height, icing and the like for each sea area, with the area's outline;
- opening bridges, locks and lock canals (Väylävirasto's waterway data);
- the speed limits a planned route runs through (Väylävirasto's restriction areas, the
  ones the drawn chart shows).

Everything is fetched only when the panel asks, kept for a while, and small: lists in
memory, the waterway structures also on disk since they change a few times a year.

Checked against the real services: the weather warning feed, and that Väylävirasto has
these layers. Not checked (this was written where they can't be reached): the nautical
warnings' field names and the fields of the bridges, locks and canals, so those are read
loosely, by what a field's name contains, and anything not recognised is still shown.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from .config import USER_AGENT
from .geo import distance_m, point_in_polygon

_LOGGER = logging.getLogger(__name__)

# Nautical warnings left Digitraffic in early 2026; since 13 Jan 2026 Traficom publishes them
# in its open spatial data service, as three layers by shape.
TRAFICOM_OPEN_WFS = "https://julkinen.traficom.fi/inspirepalvelu/avoin/wfs"
NAUTICAL_LAYERS = ("avoin:navigational_warnings_p", "avoin:navigational_warnings_l", "avoin:navigational_warnings_a")
CAP_URL = "https://alerts.fmi.fi/cap/feed/atom_en-GB.xml"
VAYLA_WFS = "https://avoinapi.vaylapilvi.fi/vaylatiedot/vesivaylatiedot/wfs"
STRUCTURES = {  # kind -> Väylävirasto layer
    "bridge": "vesivaylatiedot:sulkukanavat_avattavat_sillat",
    "lock": "vesivaylatiedot:sulkukanavat_sulut",
    "canal": "vesivaylatiedot:sulkukanavat_uusi",
}
RESTRICT_TYPE = "vesivaylatiedot:rajoitusalue_a_uusi"
AREA = (19.0, 59.3, 31.6, 70.1)  # lon/lat box: Finland's waters, sea and lakes
TIMEOUT = 25
RETRY = 5 * 60
NAUTICAL_KEEP = 15 * 60
WEATHER_KEEP = 10 * 60
STRUCTURES_KEEP = 14 * 86400
LIMITS_KEEP = 6 * 3600
MAX_SHAPE_POINTS = 400
MAX_WARNINGS = 300
SAMPLE_M = 60  # a route is checked against the speed limits at points this far apart
KMH_PER_KN = 1.852
SPEED_LIMIT_CODE = "01"  # in rajoitustyypit, as the drawn chart reads it

Fetch = Callable[[], Awaitable[Any]]


def _ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        text = value.strip()
        for candidate in (text.replace("Z", "+00:00"), text.rstrip("Z"), text[:19], text[:10]):
            try:
                return datetime.fromisoformat(candidate).timestamp()
            except ValueError:
                continue
        if re.fullmatch(r"\d{10,13}", text):
            return _ts(int(text))
    return None


def _thin(points: list[list[float]], limit: int = MAX_SHAPE_POINTS) -> list[list[float]]:
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def _lonlat(c: Any, mercator: bool) -> list[float] | None:
    try:
        x, y = float(c[0]), float(c[1])
    except (TypeError, ValueError, IndexError):
        return None
    if mercator:
        lon = x / 20037508.342789244 * 180
        lat = math.degrees(2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2)
    else:
        lon, lat = x, y
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return [round(lat, 6), round(lon, 6)]


def shapes_of(geometry: Any, mercator: bool = False) -> list[dict[str, Any]]:
    """A GeoJSON geometry of any kind as [{"kind": "point" | "line" | "area", "points":
    [[lat, lon], ...]}] (an area's outer ring only), long ones thinned."""
    out: list[dict[str, Any]] = []
    if not isinstance(geometry, dict):
        return out
    kind, coords = geometry.get("type"), geometry.get("coordinates")

    def ring(points: Any, as_kind: str) -> None:
        pts = [p for p in (_lonlat(c, mercator) for c in points or []) if p]
        if pts and (as_kind == "point" or len(pts) >= 2):
            out.append({"kind": as_kind, "points": _thin(pts)})

    if kind == "Point":
        ring([coords], "point")
    elif kind == "MultiPoint":
        for c in coords or []:
            ring([c], "point")
    elif kind == "LineString":
        ring(coords, "line")
    elif kind == "MultiLineString":
        for line in coords or []:
            ring(line, "line")
    elif kind == "Polygon":
        if coords:
            ring(coords[0], "area")
    elif kind == "MultiPolygon":
        for poly in coords or []:
            if poly:
                ring(poly[0], "area")
    elif kind == "GeometryCollection":
        for g in geometry.get("geometries") or []:
            out.extend(shapes_of(g, mercator))
    return out


def centre_of(shapes: list[dict[str, Any]]) -> list[float] | None:
    pts = [p for s in shapes for p in s["points"]]
    if not pts:
        return None
    return [round(sum(p[0] for p in pts) / len(pts), 6), round(sum(p[1] for p in pts) / len(pts), 6)]


def _in_area(point: list[float] | None, area: tuple[float, float, float, float] = AREA) -> bool:
    return bool(point) and area[1] <= point[0] <= area[3] and area[0] <= point[1] <= area[2]


def _first(props: dict[str, Any], *names: str) -> Any:
    """The first of these properties that has a value; names are matched loosely (case and
    underscores don't matter), English before Finnish before Swedish as listed."""
    low = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in props.items()}
    for name in names:
        v = low.get(re.sub(r"[^a-z0-9]", "", name.lower()))
        if v not in (None, "", [], {}):
            return v
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value if v not in (None, ""))
    text = re.sub(r"[ \t]+", " ", str(value)).strip()
    return text or None


# --- nautical warnings ---------------------------------------------------------------------

_LANG_ORDER = ("en", "eng", "fi", "fin", "sv", "swe", "se")
_seen_fields: set[tuple[str, ...]] = set()


def _like(props: dict[str, Any], *words: str, skip: tuple[str, ...] = ()) -> tuple[str | None, Any]:
    """(key, value) of the property whose name holds one of these words, earlier words first;
    among the same field in several languages English, then Finnish, then Swedish. Traficom's
    field names for the warnings aren't published, so they are found by what they are called."""
    def lang(key: str) -> int:
        tail = re.split(r"[^a-z]+", key.lower())[-1]
        tail = tail if tail in _LANG_ORDER else key.lower()[-2:]
        return _LANG_ORDER.index(tail) if tail in _LANG_ORDER else len(_LANG_ORDER)
    for word in words:
        found = [k for k, v in props.items() if word in k.lower() and v not in (None, "", [], {})
                 and not any(x in k.lower() for x in skip)]
        if found:
            key = min(found, key=lambda k: (lang(k), len(k)))
            return key, props[key]
    return None, None


def parse_nautical(data: Any, now: float | None = None) -> list[dict[str, Any]]:
    now = now or time.time()
    mercator = bool((data or {}).get("mercator"))
    out = []
    for feature in (data or {}).get("features") or []:
        props = feature.get("properties") or {}
        shapes = shapes_of(feature.get("geometry"), mercator=mercator)
        at = centre_of(shapes)
        if not _in_area(at):
            continue
        fields = tuple(sorted(props))
        if fields not in _seen_fields and len(_seen_fields) < 6:  # for tuning the guesses, via Copy diagnostics
            _seen_fields.add(fields)
            _LOGGER.info("Nautical warnings: fields in the data: %s", ", ".join(fields))
        used: set[str | None] = set()

        def pick(*words: str, skip: tuple[str, ...] = ()) -> Any:
            key, value = _like(props, *words, skip=skip)
            used.add(key)
            return value

        dates = ("time", "date", "pvm", "aika")
        end = _ts(pick("validityend", "validity_end", "endtime", "end_time", "loppu", "paatty", "until", "valid_to", "validto",
                       "expir", "cancel"))
        if end and end < now:
            continue
        item = {
            "id": pick("warningid", "warning_id") or feature.get("id") or props.get("id"),
            "number": pick("number", "numero", "nro", skip=dates),
            "type": _text(pick("type", "tyyppi", "category", "luokka")) or "Nautical warning",
            "area": _text(pick("areas", "area", "alue", "sea")),
            "place": _text(pick("location", "sijainti", "paikka", "place", "position")),
            "text": _text(pick("contents", "content", "sisalto", "sisältö", "text", "teksti", "description", "kuvaus",
                               "message", "viesti")),
            "start": _ts(pick("validitystart", "validity_start", "starttime", "start_time", "alku", "valid_from", "validfrom",
                              "start")),
            "end": end,
            "published": _ts(pick("publish", "julkai", "creat", "issue", "luonti")),
            "at": at,
            "shapes": shapes,
        }
        item["text_fi"] = item["text"]
        # Whatever else the data says, so nothing is lost if a guess above missed.
        item["props"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in list(props.items())[:40]
                         if k not in used and isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != ""
                         and not re.search(r"(?i)^(objectid|fid|gid|shape|geom)", k) and len(str(v)) <= 600}
        out.append(item)
        if len(out) >= MAX_WARNINGS:
            break
    out.sort(key=lambda w: -(w["published"] or w["start"] or 0))
    return out


# --- marine weather warnings (CAP) -----------------------------------------------------------

def _local(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in el if _local(c) == name), None)


def _child_text(el: ET.Element, name: str) -> str | None:
    c = _child(el, name)
    return c.text.strip() if c is not None and c.text and c.text.strip() else None


def parse_cap(body: bytes | str, now: float | None = None) -> list[dict[str, Any]]:
    """The sea warnings in FMI's CAP feed: every alert whose event code starts with "sea"
    (seaWind, seaWaveHeight, seaIcing, ...), from its English text, with its areas."""
    now = now or time.time()
    root = ET.fromstring(body)
    out = []
    for alert in root.iter():
        if _local(alert) != "alert":
            continue
        infos = [i for i in alert if _local(i) == "info"]
        info = next((i for i in infos if (_child_text(i, "language") or "").lower().startswith("en")), None)
        if info is None:
            if not infos:
                continue
            info = infos[0]
        code = None
        for ec in info:
            if _local(ec) == "eventCode":
                code = _child_text(ec, "value") or code
        if not code or not code.lower().startswith("sea"):
            continue
        onset, expires = _ts(_child_text(info, "onset")), _ts(_child_text(info, "expires"))
        if expires and expires < now:
            continue
        params = {}
        for p in info:
            if _local(p) == "parameter":
                name, value = _child_text(p, "valueName"), _child_text(p, "value")
                if name and value:
                    params[name] = value
        areas = []
        for a in info:
            if _local(a) != "area":
                continue
            rings = []
            for poly in a:
                if _local(poly) != "polygon" or not poly.text:
                    continue
                pts = []
                for pair in poly.text.split():
                    try:
                        lat, lon = (float(v) for v in pair.split(","))
                    except ValueError:
                        continue
                    pts.append([round(lat, 5), round(lon, 5)])
                if len(pts) >= 3:
                    rings.append(_thin(pts))
            if rings:
                areas.append({"name": _child_text(a, "areaDesc"), "rings": rings})
        if not areas:
            continue

        def number(key: str) -> float | None:
            try:
                return float(params[key])
            except (KeyError, ValueError):
                return None

        out.append({
            "id": _child_text(alert, "identifier"),
            "code": code,
            "event": _child_text(info, "event") or "Warning for sea areas",
            "headline": _child_text(info, "headline"),
            "text": _child_text(info, "description"),
            "severity": _child_text(info, "severity"),
            "color": (params.get("color") or "yellow").lower(),
            "wind_ms": number("windIntensity"), "wind_dir": number("windDirection"),
            "wave_m": number("waveHeight"),
            "onset": onset, "expires": expires,
            "areas": areas,
        })
    rank = {"red": 0, "orange": 1, "yellow": 2}
    out.sort(key=lambda w: (rank.get(w["color"], 3), w["onset"] or 0))
    return out


def warnings_at(warnings: list[dict[str, Any]], lat: float, lon: float, now: float | None = None) -> list[dict[str, Any]]:
    """The sea warnings whose area holds a place, in force now or still to come."""
    now = now or time.time()
    hits = []
    for w in warnings:
        if w.get("expires") and w["expires"] < now:
            continue
        for area in w["areas"]:
            if any(point_in_polygon(lat, lon, ring) for ring in area["rings"]):
                hits.append({k: v for k, v in w.items() if k != "areas"} | {"area": area["name"]})
                break
    return hits


# --- opening bridges, locks, canals -----------------------------------------------------------

def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        n = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _measure(props: dict[str, Any], *words: str, not_words: tuple[str, ...] = ()) -> float | None:
    """The first positive number in a property whose name holds one of these words."""
    for key, value in props.items():
        k = key.lower()
        if any(w in k for w in words) and not any(w in k for w in not_words):
            n = _number(value)
            if n and 0 < n < 1000:
                return round(n, 2)
    return None


def parse_structures(data: Any, kind: str) -> list[dict[str, Any]]:
    out = []
    for feature in (data or {}).get("features") or []:
        props = {k: v for k, v in (feature.get("properties") or {}).items()
                 if isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != ""}
        shapes = shapes_of(feature.get("geometry"), mercator=True)
        at = centre_of(shapes)
        if not _in_area(at):
            continue
        name = None
        for key in props:
            k = key.lower()
            if isinstance(props[key], str) and ("nimi" in k or "name" in k or "namn" in k):
                name = props[key].strip()
                if "fi" in k or k in ("nimi", "name"):
                    break
        item = {
            "kind": kind, "at": at, "name": name or None,
            # Clearance under a bridge, and what fits through a lock or canal, in metres,
            # where the data has them under a name like alikulkukorkeus, leveys, syvyys.
            "height_m": _measure(props, "alikulku", "korkeus", "height", "clearance"),
            "width_m": _measure(props, "leveys", "width"),
            "length_m": _measure(props, "pituus", "length"),
            "depth_m": _measure(props, "syvyys", "syvays", "depth", "draught"),
            "props": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in list(props.items())[:40]
                      if not re.search(r"(?i)^(objectid|fid|id|shape|geom|gid)", k) and len(str(v)) <= 300},
        }
        lines = [s for s in shapes if s["kind"] != "point"]
        if kind == "canal" and lines:
            item["shapes"] = lines
        out.append(item)
    return out


# --- speed limits along a route -------------------------------------------------------------------

def parse_limits(data: Any) -> list[dict[str, Any]]:
    """Speed limit areas [{kmh, ring}] from Väylävirasto's restriction areas."""
    out = []
    for feature in (data or {}).get("features") or []:
        props = feature.get("properties") or {}
        types = str(_first(props, "rajoitustyypit", "rajoitustyyppi") or "")
        kmh = _number(_first(props, "suuruus"))
        if not kmh or not re.search(rf"(^|\D){SPEED_LIMIT_CODE}(\D|$)", types):
            continue
        for shape in shapes_of(feature.get("geometry"), mercator=True):
            if shape["kind"] == "area" and len(shape["points"]) >= 3:
                out.append({"kmh": kmh, "ring": shape["points"]})
    return out


def route_samples(points: list[list[float]], step_m: float = SAMPLE_M) -> list[tuple[float, float, float]]:
    """(lat, lon, metres from the start) along a route, a point every step_m at most."""
    out: list[tuple[float, float, float]] = []
    along = 0.0
    for a, b in zip(points, points[1:]):
        d = distance_m(a[0], a[1], b[0], b[1])
        n = max(1, math.ceil(d / step_m))
        for i in range(n):
            f = i / n
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, along + d * f))
        along += d
    if points:
        out.append((points[-1][0], points[-1][1], along))
    return out


def limits_on_route(points: list[list[float]], areas: list[dict[str, Any]], speed_kn: float | None = None) -> dict[str, Any]:
    """The stretches of a route inside speed limit areas: [{from_m, to_m, kmh, kn, line}]
    in route order (the lowest limit where areas overlap), and how many minutes they add at
    the planned speed."""
    samples = route_samples(points)
    boxes = []
    for area in areas:
        lats, lons = [p[0] for p in area["ring"]], [p[1] for p in area["ring"]]
        boxes.append((min(lats), min(lons), max(lats), max(lons)))
    limit_at: list[float | None] = []
    for lat, lon, _ in samples:
        best = None
        for area, (s, w, n, e) in zip(areas, boxes):
            if s <= lat <= n and w <= lon <= e and (best is None or area["kmh"] < best) \
                    and point_in_polygon(lat, lon, area["ring"]):
                best = area["kmh"]
        limit_at.append(best)
    stretches: list[dict[str, Any]] = []
    start = None
    for i in range(len(samples) + 1):
        cur = limit_at[i] if i < len(samples) else None
        if start is not None and cur != limit_at[start]:
            a, b = samples[start], samples[min(i, len(samples) - 1)]
            kmh = limit_at[start]
            if b[2] - a[2] >= 1:
                line = [[round(p[0], 6), round(p[1], 6)] for p in samples[start:i + 1]]
                stretches.append({"from_m": round(a[2]), "to_m": round(b[2]), "kmh": kmh,
                                  "kn": round(kmh / KMH_PER_KN, 1), "line": _thin(line, 200)})
            start = None
        if start is None and cur is not None:
            start = i
    extra_s = 0.0
    if speed_kn:
        for s in stretches:
            if speed_kn > s["kn"] > 0:
                nm = (s["to_m"] - s["from_m"]) / 1852
                extra_s += (nm / s["kn"] - nm / speed_kn) * 3600
    return {"stretches": stretches, "extra_min": round(extra_s / 60, 1) if speed_kn else None,
            "length_m": round(samples[-1][2]) if samples else 0}


# --- fetching -----------------------------------------------------------------------------------

class _Kept:
    """One source's parsed answer, kept for a while; a failure keeps the previous one."""

    def __init__(self, name: str, keep: float, fetch: Fetch, parse: Callable[[Any], Any]):
        self.name, self.keep, self.fetch, self.parse = name, keep, fetch, parse
        self.value: Any = []
        self.fetched = 0.0
        self.ok_at: float | None = None
        self.error: str | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Any:
        async with self._lock:
            if time.time() - self.fetched < self.keep:
                return self.value
            try:
                self.value = self.parse(await self.fetch())
                self.fetched = self.ok_at = time.time()
                self.error = None
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, AttributeError, KeyError, ET.ParseError) as err:
                if self.error is None:
                    _LOGGER.warning("Could not read the %s: %s", self.name, err)
                self.error = str(err) or type(err).__name__
                self.fetched = time.time() - self.keep + RETRY
            return self.value

    def status(self) -> dict[str, Any]:
        return {"ok_at": self.ok_at, "error": self.error, "count": len(self.value) if isinstance(self.value, list) else None}


class Notices:
    def __init__(self, directory: Path, session: aiohttp.ClientSession | None, fake: dict[str, Any] | None = None):
        self.dir = directory
        self.session = session
        self.fake = fake or {}
        self.nautical = _Kept("nautical warnings", NAUTICAL_KEEP, self._nautical, parse_nautical)
        self.weather = _Kept("marine weather warnings", WEATHER_KEEP, self._cap, parse_cap)
        self._structures: list[dict[str, Any]] | None = None
        self._structures_at = 0.0
        self.structures_error: str | None = None
        self._slock = asyncio.Lock()
        self._limits: dict[tuple[int, int, int, int], tuple[float, list[dict[str, Any]]]] = {}
        self.limits_error: str | None = None

    async def _json(self, url: str, params: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> Any:
        async with self.session.get(url, params=params, headers=headers or {"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _nautical(self) -> Any:
        if "nautical" in self.fake:
            return self.fake["nautical"]()
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "srsName": "EPSG:3857",
                  "outputFormat": "application/json", "count": "2000"}
        answers = await asyncio.gather(*(self._json(TRAFICOM_OPEN_WFS, {**params, "typeNames": layer})
                                         for layer in NAUTICAL_LAYERS), return_exceptions=True)
        good = [a for a in answers if isinstance(a, dict)]
        if not good:  # all three failed: say why
            raise next(a for a in answers if isinstance(a, BaseException))
        for layer, a in zip(NAUTICAL_LAYERS, answers):
            if isinstance(a, BaseException):
                _LOGGER.warning("Nautical warnings: %s could not be read: %s", layer, a)
        return {"mercator": True, "features": [f for a in good for f in a.get("features") or []]}

    async def _cap(self) -> Any:
        if "cap" in self.fake:
            return self.fake["cap"]()
        async with self.session.get(CAP_URL, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _wfs(self, type_name: str, bbox: tuple[float, float, float, float] | None = None) -> Any:
        if "wfs" in self.fake:
            return self.fake["wfs"](type_name, bbox)
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": type_name,
                  "srsName": "EPSG:3857", "outputFormat": "application/json"}
        if bbox:
            params["bbox"] = ",".join(f"{v:.1f}" for v in bbox) + ",EPSG:3857"
        return await self._json(VAYLA_WFS, params)

    async def structures(self) -> list[dict[str, Any]]:
        """Opening bridges, locks and lock canals: all of Finland's, a few hundred at most,
        kept on disk and read again every two weeks."""
        async with self._slock:
            path = self.dir / "structures.json.gz"
            if self._structures is None:
                try:
                    saved = json.loads(gzip.decompress(await asyncio.to_thread(path.read_bytes)))
                    self._structures, self._structures_at = saved["items"], saved["at"]
                except (OSError, ValueError, KeyError):
                    self._structures, self._structures_at = [], 0.0
            if time.time() - self._structures_at < STRUCTURES_KEEP:
                return self._structures
            items: list[dict[str, Any]] = []
            try:
                for kind, type_name in STRUCTURES.items():
                    items.extend(parse_structures(await self._wfs(type_name), kind))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, AttributeError, KeyError) as err:
                if self.structures_error is None:
                    _LOGGER.warning("Could not read the bridges, locks and canals: %s", err)
                self.structures_error = str(err) or type(err).__name__
                self._structures_at = time.time() - STRUCTURES_KEEP + RETRY
                return self._structures
            self.structures_error = None
            self._structures, self._structures_at = items, time.time()
            body = gzip.compress(json.dumps({"at": self._structures_at, "items": items}).encode())
            try:
                await asyncio.to_thread(self.dir.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(path.write_bytes, body)
            except OSError as err:
                _LOGGER.warning("Could not keep the bridges, locks and canals: %s", err)
            return items

    async def route_limits(self, points: list[list[float]], speed_kn: float | None) -> dict[str, Any]:
        """The speed limits a route runs through. The restriction areas are asked for in
        squares of about 0.2 degrees round the route and kept for some hours."""
        cells = set()
        for lat, lon, _ in route_samples(points, 2000):
            cells.add((math.floor(lat * 5), math.floor(lon * 5)))
        if len(cells) > 400:
            raise ValueError("The route is too long to check")
        areas: list[dict[str, Any]] = []
        seen = set()
        todo = sorted(cells)
        # Neighbouring cells are asked for together, in boxes of up to 5 x 5 cells.
        boxes: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for cy, cx in todo:
            boxes.setdefault((cy // 5, cx // 5), []).append((cy, cx))
        now = time.time()
        for (by, bx), members in boxes.items():
            key = (by, bx, 5, 5)
            kept = self._limits.get(key)
            if not kept or now - kept[0] > LIMITS_KEEP:
                south, west, north, east = by, bx, by + 1, bx + 1  # in degrees: 5 cells of 0.2
                box = (*_merc(south, west), *_merc(north, east))
                try:
                    found = parse_limits(await self._wfs(RESTRICT_TYPE, (box[0], box[1], box[2], box[3])))
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, AttributeError, KeyError) as err:
                    self.limits_error = str(err) or type(err).__name__
                    raise
                self.limits_error = None
                kept = (now, found)
                self._limits[key] = kept
                if len(self._limits) > 60:
                    self._limits.pop(next(iter(self._limits)))
            for area in kept[1]:
                mark = (area["kmh"], tuple(area["ring"][0]), len(area["ring"]))
                if mark not in seen:
                    seen.add(mark)
                    areas.append(area)
        return limits_on_route(points, areas, speed_kn)

    def status(self) -> dict[str, Any]:
        return {"nautical": self.nautical.status(), "weather": self.weather.status(),
                "structures": {"error": self.structures_error, "count": len(self._structures or [])},
                "limits": {"error": self.limits_error}}


def _merc(lat: float, lon: float) -> tuple[float, float]:
    x = lon * 20037508.342789244 / 180
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * 6378137.0
    return x, y
