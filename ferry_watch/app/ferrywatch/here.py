"""What's here: a long press on the map asks what the app knows about one place.

- the chart's depth there (the depth area it lies in, from the drawn chart's blocks on
  disk: Traficom's DRVAL1-DRVAL2, e.g. "3-6 m"), or that it's land;
- the weather now and the next hours there (Open-Meteo's point forecast and marine
  forecast: wind, gusts, air temperature, waves, sea temperature), kept 30 minutes;
- the seabed type there (GTK's seabed map, identify), kept a month.

Each part is optional: what can't be had is left out, with the reason.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import math
import os
import time
from typing import Any

import aiohttp

from .config import USER_AGENT

_LOGGER = logging.getLogger(__name__)

FORECAST = "https://api.open-meteo.com/v1/forecast"
MARINE = "https://marine-api.open-meteo.com/v1/marine"
GTK_IDENTIFY = "https://gtkdata.gtk.fi/arcgis/rest/services/Maankamara/Merenpohja/MapServer/identify"
WX_TTL = 1800
SEABED_TTL = 30 * 86400


def _inside(x: float, y: float, pts: list[tuple[int, int]]) -> bool:
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def depth_at(vchart: Any, lat: float, lon: float) -> dict[str, Any] | None:
    """The drawn chart's depth area at a place: {"min": m, "max": m} (max None for "and
    deeper"), {"land": True} inside land, or None where the chart has no block yet."""
    from . import vchart as vc

    n = 2 ** vc.BLOCK_Z
    bx = int((lon + 180) / 360 * n)
    by = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    path = vchart._path(bx, by)
    if not path.exists():
        return None
    try:
        layers = vc.read_block(path, 1, ("depth",))[1]
    except (OSError, ValueError, EOFError):
        return None
    depth = layers.get("depth")
    if depth is None or not depth.n:
        return None
    x, y = vc.to_block(bx, by)(*vc.lonlat_to_mercator(lon, lat))
    best = None
    best_area = None
    for i in range(depth.n):
        x0, y0, x1, y1 = depth.bbox(i)
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            continue
        rings = list(depth.rings(i))
        outer = depth.pts(depth.vo[rings[0]], depth.vo[rings[0] + 1])
        if len(outer) < 3 or not _inside(x, y, outer):
            continue
        if any(_inside(x, y, depth.pts(depth.vo[r], depth.vo[r + 1])) for r in rings[1:]):
            continue  # in a hole: a smaller area lies there
        area = (x1 - x0) * (y1 - y0)
        if best_area is None or area < best_area:
            best, best_area = i, area
    if best is None:
        return {"land": True}
    props = depth.props[depth.pi[best]] or []

    def num(v: Any) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    lo = num(props[0]) if props else None
    hi = num(props[1]) if len(props) > 1 else None
    if lo is not None and lo < 0 and (hi is None or hi <= 0):
        return {"drying": True, "min": lo, "max": hi}
    return {"min": lo, "max": hi if hi is None or hi < 9000 else None}


class Here:
    def __init__(self, session: aiohttp.ClientSession, vchart: Any, fake: bool = False, harbours: Any = None):
        self.session = session
        self.harbours = harbours
        self.vchart = vchart
        self.fake = fake
        self._wx: dict[tuple, tuple[float, dict]] = {}
        self._seabed: dict[tuple, tuple[float, Any]] = {}

    async def _get(self, url: str, params: dict) -> Any:
        async with self.session.get(url, params=params, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def weather(self, lat: float, lon: float) -> dict[str, Any]:
        key = (round(lat * 20) / 20, round(lon * 20) / 20)
        hit = self._wx.get(key)
        if hit and time.time() - hit[0] < WX_TTL:
            return hit[1]
        if self.fake:
            out = {"wind": 6.5, "wind_dir": 225, "gust": 10.2, "temp": 7.4, "wave": 0.6, "wave_dir": 220,
                   "sea_temp": 9.1, "code": 3,
                   "hours": [{"t": int(time.time() // 3600 * 3600 + 3600 * h), "wind": 6 + h * 0.4, "gust": 9 + h * 0.5,
                              "dir": 220 + h * 3, "wave": 0.5 + h * 0.05} for h in range(1, 13, 2)]}
        else:
            common = {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": "UTC", "forecast_hours": "12"}
            out: dict[str, Any] = {}
            try:
                f = await self._get(FORECAST, {**common, "wind_speed_unit": "ms",
                                               "current": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,temperature_2m,weather_code",
                                               "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m"})
                cur = f.get("current") or {}
                out.update(wind=cur.get("wind_speed_10m"), wind_dir=cur.get("wind_direction_10m"),
                           gust=cur.get("wind_gusts_10m"), temp=cur.get("temperature_2m"), code=cur.get("weather_code"))
                hourly = f.get("hourly") or {}
                times = hourly.get("time") or []
                hours = []
                for i, t in enumerate(times):
                    try:
                        ts = calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%M"))
                    except ValueError:
                        continue
                    if i % 2 == 1:
                        hours.append({"t": ts, "wind": hourly["wind_speed_10m"][i], "gust": hourly["wind_gusts_10m"][i],
                                      "dir": hourly["wind_direction_10m"][i]})
                out["hours"] = hours
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError) as err:
                out["error"] = str(err) or type(err).__name__
            try:
                m = await self._get(MARINE, {**common, "current": "wave_height,wave_direction,sea_surface_temperature"})
                cur = m.get("current") or {}
                out.update(wave=cur.get("wave_height"), wave_dir=cur.get("wave_direction"),
                           sea_temp=cur.get("sea_surface_temperature"))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                _LOGGER.debug("Marine forecast at %s,%s: %s", lat, lon, err)
        self._wx[key] = (time.time(), out)
        if len(self._wx) > 200:
            self._wx.pop(next(iter(self._wx)))
        return out

    async def seabed(self, lat: float, lon: float) -> Any:
        key = (round(lat, 3), round(lon, 3))
        hit = self._seabed.get(key)
        if hit and time.time() - hit[0] < SEABED_TTL:
            return hit[1]
        if self.fake:
            out = {"type": "Clay", "layer": "Seabed sediments 1:20 000"}
        else:
            d = 0.01
            params = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "sr": "4326",
                      "layers": "all", "tolerance": "1", "returnGeometry": "false", "f": "json",
                      "mapExtent": f"{lon - d},{lat - d},{lon + d},{lat + d}", "imageDisplay": "256,256,96"}
            try:
                data = await self._get(GTK_IDENTIFY, params)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                return {"error": str(err) or type(err).__name__}
            out = None
            for res in data.get("results") or []:
                attrs = res.get("attributes") or {}
                # The class in words: the display field, else the first text attribute that isn't an id.
                value = res.get("value")
                if not value or str(value).replace(".", "").isdigit():
                    value = next((v for k, v in attrs.items() if isinstance(v, str) and v.strip()
                                  and not k.lower().startswith(("objectid", "shape", "id", "fid"))
                                  and not v.replace(".", "").isdigit()), None)
                if value:
                    out = {"type": str(value)[:80], "layer": res.get("layerName", "")}
                    break
        self._seabed[key] = (time.time(), out)
        if len(self._seabed) > 500:
            self._seabed.pop(next(iter(self._seabed)))
        return out

    async def at(self, lat: float, lon: float) -> dict[str, Any]:
        depth = await asyncio.to_thread(depth_at, self.vchart, lat, lon) if self.vchart else None
        wx, bed = await asyncio.gather(self.weather(lat, lon), self.seabed(lat, lon) if not (depth or {}).get("land") else _none())
        try:
            near = self.harbours.nearest(lat, lon) if self.harbours else []
        except Exception as err:  # noqa: BLE001 - nice to have, never in the way
            _LOGGER.debug("Nearest harbours: %s", err)
            near = []
        return {"lat": lat, "lon": lon, "depth": depth, "weather": wx, "seabed": bed, "harbours": near}


async def _none() -> None:
    return None


def fake_enabled() -> bool:
    return os.environ.get("FW_SIMULATE") == "1"
