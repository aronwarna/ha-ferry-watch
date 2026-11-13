"""Your own pins (fishing spots, anchorages, saunas, notes) and place search.

Pins live in the kv table as one small list, so they follow you between the phone and
the desktop and go into Home Assistant's backups. Place search asks OpenStreetMap's
Nominatim (islands, bays, skerries, villages; Finland, Åland, Sweden and Estonia),
through the NAS, one request at a time and kept a day, as its usage policy asks.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from typing import Any
from xml.sax.saxutils import escape

import aiohttp

from .config import USER_AGENT

NOMINATIM = "https://nominatim.openstreetmap.org/search"
KINDS = ("pin", "fish", "anchor", "sauna", "swim", "camp", "danger", "photo", "home")
MAX_PINS = 2000


def clean_pin(body: dict[str, Any], old: dict[str, Any] | None = None) -> dict[str, Any]:
    pin = dict(old or {})
    try:
        lat, lon = float(body.get("lat", pin.get("lat"))), float(body.get("lon", pin.get("lon")))
    except (TypeError, ValueError):
        raise ValueError("lat and lon are needed") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("not a place")
    kind = body.get("kind", pin.get("kind", "pin"))
    pin.update(
        id=pin.get("id") or uuid.uuid4().hex[:12], lat=round(lat, 6), lon=round(lon, 6),
        name=str(body.get("name", pin.get("name", "")) or "").strip()[:80],
        note=str(body.get("note", pin.get("note", "")) or "").strip()[:2000],
        kind=kind if kind in KINDS else "pin",
        ts=pin.get("ts") or int(time.time()),
    )
    return pin


def pins_gpx(pins: list[dict[str, Any]]) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="Ferry Watch" xmlns="http://www.topografix.com/GPX/1/1">']
    for p in pins:
        out.append(f'  <wpt lat="{p["lat"]}" lon="{p["lon"]}"><name>{escape(p.get("name") or p["kind"])}</name>'
                   f'<desc>{escape(p.get("note", ""))}</desc><sym>{escape(p["kind"])}</sym></wpt>')
    out.append("</gpx>")
    return "\n".join(out) + "\n"


GARMIN_SYMBOLS = {"fish": "fish", "anchor": "anchor", "danger": "danger", "hazard": "danger", "swim": "swim",
                  "camp": "camp", "home": "home", "residence": "home", "scenic": "photo", "photo": "photo"}


def gpx_waypoints(text: str) -> list[dict[str, Any]]:
    """Waypoints (<wpt>) of a GPX file as pin bodies; a Garmin symbol name picks the kind.
    Entities are refused (no DOCTYPE), so a hostile file can't blow up."""
    import xml.etree.ElementTree as ET

    if "<!DOCTYPE" in text[:2000].upper() or "<!ENTITY" in text.upper():
        raise ValueError("not a plain GPX file")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as err:
        raise ValueError(f"not a GPX file ({err})") from None
    out = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "wpt":
            continue
        kids = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in el}
        sym = kids.get("sym", "").lower()
        kind = next((k for word, k in GARMIN_SYMBOLS.items() if word in sym), sym if sym in KINDS else "pin")
        out.append({"lat": el.get("lat"), "lon": el.get("lon"), "name": kids.get("name", ""),
                    "note": kids.get("desc") or kids.get("cmt", ""), "kind": kind})
    return out


def parse_coords(q: str) -> tuple[float, float] | None:
    """60.1234 24.5678, 60°07.4'N 24°34.1'E, 60 07.4 N 24 34.1 E -> (lat, lon)."""
    nums = re.findall(r"-?\d+(?:[.,]\d+)?", q)
    vals = [float(n.replace(",", ".")) for n in nums]
    if len(vals) == 2:
        lat, lon = vals
    elif len(vals) == 4:
        lat, lon = vals[0] + vals[1] / 60, vals[2] + vals[3] / 60
    elif len(vals) == 6:
        lat, lon = vals[0] + vals[1] / 60 + vals[2] / 3600, vals[3] + vals[4] / 60 + vals[5] / 3600
    else:
        return None
    if re.search(r"[sS]\b", q):
        lat = -abs(lat)
    if re.search(r"[wW]\b", q):
        lon = -abs(lon)
    if -90 <= lat <= 90 and -180 <= lon <= 180 and abs(lat) > 1:
        return lat, lon
    return None


class Places:
    def __init__(self, session: aiohttp.ClientSession, fake: bool = False):
        self.session = session
        self.fake = fake
        self._cache: dict[str, tuple[float, list]] = {}
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.error: str | None = None

    async def search(self, q: str) -> list[dict[str, Any]]:
        q = q.strip()[:80]
        if len(q) < 2:
            return []
        hit = self._cache.get(q.lower())
        if hit and time.time() - hit[0] < 86400:
            return hit[1]
        if self.fake:
            from .simmaps import fake_places
            rows = fake_places(q)
        else:
            async with self._lock:  # one request a second at most
                wait = 1.0 - (time.time() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.time()
                try:
                    async with self.session.get(NOMINATIM, params={
                        "q": q, "format": "jsonv2", "limit": "10", "countrycodes": "fi,ax,se,ee",
                        "accept-language": "fi,sv,en"}, headers={"User-Agent": USER_AGENT},
                            timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        resp.raise_for_status()
                        data = await resp.json(content_type=None)
                    self.error = None
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                    self.error = str(err) or type(err).__name__
                    return []
            rows = []
            for r in data or []:
                try:
                    rows.append({"name": r.get("name") or r["display_name"].split(",")[0],
                                 "where": ", ".join(r.get("display_name", "").split(", ")[1:3]),
                                 "kind": (r.get("type") or r.get("addresstype") or "").replace("_", " "),
                                 "lat": float(r["lat"]), "lon": float(r["lon"])})
                except (KeyError, ValueError):
                    continue
        self._cache[q.lower()] = (time.time(), rows)
        if len(self._cache) > 300:
            self._cache.pop(next(iter(self._cache)))
        return rows


def fake_on() -> bool:
    return os.environ.get("FW_SIMULATE") == "1"
