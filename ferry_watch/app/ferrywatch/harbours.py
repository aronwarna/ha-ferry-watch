"""Harbours and marinas along the coast from OpenStreetMap (© OpenStreetMap contributors,
ODbL), with their services: fuel, toilets, shower, sauna, electricity, water, waste,
slipway, restaurant and guest berths.

Fetched from the Overpass API only when the panel first asks for harbours, then once a
month, into a small gzip JSON file on disk. The rows are read into memory while the panel
asks for them (about 2000 short tuples) and let go after 10 idle minutes. A failed
download keeps the old file, is logged once and is tried again after an hour.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from .coast import near_sea, overpass_poly
from .config import USER_AGENT
from .geo import distance_m

_LOGGER = logging.getLogger(__name__)

OVERPASS = "https://overpass-api.de/api/interpreter"
REFRESH = 30 * 86400
RETRY = 3600
IDLE = 600  # s without a request before the rows are let go
TIMEOUT = 180
MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 600  # per answer; the panel only asks from zoom 11
SAME_PLACE_M = 300  # one harbour mapped twice (an area and a sea mark) is shown once
CREDIT = "© OpenStreetMap contributors (ODbL)"

Fetch = Callable[[str], Awaitable[Any]]

KINDS = {  # seamark:harbour:category
    "marina": "Marina", "marina_no_facilities": "Marina (no services)", "yacht": "Yacht harbour",
    "fishing": "Fishing harbour", "ferry": "Ferry harbour", "naval": "Naval harbour",
    "roro": "Ro-ro terminal", "container": "Container terminal", "bulk": "Bulk harbour",
    "tanker": "Tanker terminal", "timber": "Timber harbour", "service": "Service harbour",
}

# Service: (OSM keys whose value isn't "no", seamark:small_craft_facility:category values)
SERVICES = {
    "guest": (("guest_berths", "harbour:visitors"), ("visitor_berth",)),
    "fuel": (("fuel", "fuel:diesel", "fuel:octane_95", "fuel:octane_98"), ("fuel_station",)),
    "toilets": (("toilets",), ("toilets",)),
    "shower": (("shower", "showers"), ("showers",)),
    "sauna": (("sauna",), ()),
    "electricity": (("electricity", "power_supply"), ("electricity",)),
    "water": (("drinking_water", "water_point", "water"), ("water_tap",)),
    "waste": (("waste_disposal", "sanitary_dump_station", "recycling"), ("refuse_bin", "pump-out")),
    "slipway": (("slipway",), ("slipway",)),
    "restaurant": (("restaurant",), ("public_inn", "restaurant")),
}


def overpass_query() -> str:
    poly = overpass_poly()
    parts = "".join(f'nwr[{f}](poly:"{poly}");' for f in
                    ('"leisure"="marina"', '"harbour"="yes"', '"seamark:type"="harbour"'))
    return f"[out:json][timeout:{TIMEOUT - 30}];({parts});out center tags;"


def _number(text: str | None) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", text or "")
    return float(m.group().replace(",", ".")) if m else None


def _link(text: str | None) -> str | None:
    text = (text or "").split(";")[0].strip()
    if not text or len(text) > 200:
        return None
    if not re.match(r"https?://", text):
        text = f"https://{text}"
    return text if re.match(r"https?://[\w.-]+\.[a-z]{2,}(/|$)", text, re.I) else None


def _services(tags: dict[str, str]) -> tuple[str, ...]:
    facilities = set(re.split(r"[;,]\s*", tags.get("seamark:small_craft_facility:category", "")))
    if tags.get("amenity") == "restaurant":
        facilities.add("restaurant")
    return tuple(key for key, (osm, sea) in SERVICES.items()
                 if any(tags.get(k, "no") not in ("no", "") for k in osm) or facilities.intersection(sea))


def parse_overpass(data: Any) -> list[list[Any]]:
    """Harbour rows [osm id, lat, lon, name, kind, [services], depth m, max draught m,
    website, phone] from an Overpass JSON answer (`out center tags`), coastal ones only,
    with a harbour mapped twice in one place merged."""
    rows: list[list[Any]] = []
    grid: dict[tuple[int, int], list[list[Any]]] = {}  # ~1 km cells, for finding doubles
    for el in (data or {}).get("elements") or []:
        tags = el.get("tags") or {}
        where = el if "lat" in el else el.get("center") or {}
        try:
            lat, lon = float(where["lat"]), float(where["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        name = (tags.get("name") or tags.get("seamark:name") or tags.get("name:fi") or tags.get("name:sv") or "").strip()
        category = tags.get("seamark:harbour:category", "").split(";")[0]
        marina = tags.get("leisure") == "marina" or category in ("marina", "marina_no_facilities", "yacht")
        if (not name and not marina) or not near_sea(lat, lon):
            continue
        kind = KINDS.get(category) or ("Marina" if marina else "Harbour")
        row = [f"{el.get('type', 'n')[:1]}{el.get('id', 0)}", round(lat, 5), round(lon, 5), name[:80], kind,
               list(_services(tags)), _number(tags.get("depth") or tags.get("seamark:harbour:depth")),
               _number(tags.get("maxdraught") or tags.get("maxdraft")),
               _link(tags.get("website") or tags.get("contact:website") or tags.get("url")),
               (tags.get("phone") or tags.get("contact:phone") or "").split(";")[0].strip()[:40] or None]
        cell = (round(lat * 100), round(lon * 50))
        twin = next((r for dy in (-1, 0, 1) for dx in (-1, 0, 1) for r in grid.get((cell[0] + dy, cell[1] + dx), ())
                     if (not name or not r[3] or r[3].lower() == name.lower())
                     and distance_m(lat, lon, r[1], r[2]) <= SAME_PLACE_M), None)
        if twin:
            if not twin[3]:
                twin[3] = row[3]
            if twin[4] in ("Harbour", "Marina") and row[4] not in ("Harbour", "Marina"):
                twin[4] = row[4]
            twin[5] = [s for s in SERVICES if s in twin[5] or s in row[5]]
            for i in (6, 7, 8, 9):
                twin[i] = twin[i] if twin[i] is not None else row[i]
            continue
        rows.append(row)
        grid.setdefault(cell, []).append(row)
    return rows


class Harbours:
    def __init__(self, path: Path, session: aiohttp.ClientSession | None, fetch: Fetch | None = None):
        self.path = path
        self.session = session
        self.fetch = fetch or self._get
        self.rows: list[tuple] | None = None  # in memory only while asked for
        self.updated = 0.0
        self.error: str | None = None
        self._failed = 0.0
        self._task: asyncio.Task | None = None
        self._used = 0.0
        self._idle_timer: asyncio.TimerHandle | None = None

    async def _get(self, query: str) -> Any:
        async with self.session.post(OVERPASS, data={"data": query}, headers={"User-Agent": USER_AGENT},
                                     timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            resp.raise_for_status()
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("answer too big")
                chunks.append(chunk)
        return json.loads(b"".join(chunks))

    def _load(self) -> None:
        try:
            with gzip.open(self.path, "rt", encoding="utf-8") as f:
                saved = json.load(f)
            self.rows = [tuple(r) for r in saved["rows"]]
            self.updated = float(saved["updated"])
        except FileNotFoundError:
            self.rows = []
        except (OSError, ValueError, KeyError, TypeError) as err:
            _LOGGER.warning("Could not read %s: %s", self.path, err)
            self.rows = []

    def _save(self, rows: list[list[Any]], updated: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"updated": updated, "source": "OpenStreetMap via Overpass API", "licence": "ODbL",
                       "rows": rows}, f, separators=(",", ":"), ensure_ascii=False)
        tmp.replace(self.path)

    async def _download(self) -> None:
        try:
            data = await self.fetch(overpass_query())
            rows = await asyncio.to_thread(parse_overpass, data)
            del data
            if not rows:
                raise ValueError("no harbours in the answer")
            now = time.time()
            await asyncio.to_thread(self._save, rows, now)
            self.rows, self.updated, self.error = [tuple(r) for r in rows], now, None
            _LOGGER.info("Harbours from OpenStreetMap: %s", len(rows))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as err:
            if self.error is None:
                _LOGGER.warning("Could not download harbours from OpenStreetMap (Overpass): %s", err)
            self.error = str(err) or type(err).__name__
            self._failed = time.time()

    def _touch(self) -> None:
        self._used = time.monotonic()
        if self._idle_timer is None:
            self._idle_timer = asyncio.get_running_loop().call_later(IDLE, self._release)

    def _release(self) -> None:
        self._idle_timer = None
        idle = time.monotonic() - self._used
        if idle >= IDLE:
            self.rows = None
        else:
            self._idle_timer = asyncio.get_running_loop().call_later(IDLE - idle, self._release)

    def in_view(self, south: float, west: float, north: float, east: float) -> dict[str, Any]:
        """Harbours inside a box, and whether a download is under way."""
        self._touch()
        if self.rows is None:
            self._load()
        now = time.time()
        stale = now - self.updated > REFRESH
        if stale and (self._task is None or self._task.done()) and now - self._failed > RETRY:
            self._task = asyncio.create_task(self._download())
        rows = [r for r in self.rows if south <= r[1] <= north and west <= r[2] <= east]
        return {"harbours": rows[:MAX_ROWS], "pending": bool(self._task and not self._task.done()),
                "error": self.error, "updated": self.updated or None}
