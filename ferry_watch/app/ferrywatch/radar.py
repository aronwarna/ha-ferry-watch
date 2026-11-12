"""The rain radar as a map overlay (card T4).

FMI's Finnish radar composite from its open WMS (openwms.fmi.fi, GeoServer), the last hour
in 5-minute frames, played on the time bar at two frames a second. FMI replaced its old
radar layers (suomi_dbz_eureffin and others, retired November 2026) with
radar_finland_cappi_*; this uses the reflectivity one, radar_finland_cappi_dbzh, which
shows snow as well as rain.

The frames come from the layer's time dimension in GetCapabilities (asked of the layer's
own small capabilities document first, the Radar namespace's if that fails); if neither
can be read, the last hour on the 5-minute clock is assumed. The same document lists the
layer's styles: the winter colour scale (from -20 dBZ, light snow shows) from October to
April, the summer one (from 8 dBZ) otherwise, each only if the server lists it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

import aiohttp

from .config import USER_AGENT
from .overlays import Overlay, Overlays, every, parse_time_dimension

_LOGGER = logging.getLogger(__name__)

ID = "rain-radar"
FRAMES = 12  # the last hour
STEP = 300  # s between frames
LAG = 600  # s a frame takes to appear, when the times have to be assumed
TIMEOUT = 20
BASE = "https://openwms.fmi.fi/geoserver"
NAMESPACE = "Radar"
LAYER = "radar_finland_cappi_dbzh"
CAPS_URLS = (f"{BASE}/{NAMESPACE}/{LAYER}/wms", f"{BASE}/{NAMESPACE}/wms")  # GeoServer layer, namespace
STYLE_WINTER = "radar_dbz_winter_-20_40"
STYLE_SUMMER = "radar_dbz_summer_8_50"
WINTER_MONTHS = {10, 11, 12, 1, 2, 3, 4}
ATTRIBUTION = "Rain radar: Finnish Meteorological Institute (CC BY 4.0)"
# FMI's radars cover Finland and the sea around it (the box of FMI's own example request).
BOUNDS = (57.0, 16.2, 71.8, 35.9)

_fallback = every(STEP, FRAMES, lag=LAG)
_NAME = r"<Name>\s*(?:{ns}:)?{layer}\s*</Name>"
_TIME = re.compile(r"<Dimension\b[^>]*\bname=\"time\"[^>]*>([^<]*)</Dimension>", re.I)
_STYLE = re.compile(r"<Style>\s*<Name>\s*([^<\s]+)\s*</Name>")


def tile_url(style: str) -> str:
    """A WMS 1.3.0 GetMap of one 256-pixel web mercator tile ({bbox} and {time} filled per tile)."""
    return (f"{BASE}/{NAMESPACE}/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={LAYER}"
            f"&STYLES={style}&CRS=EPSG:3857&BBOX={{bbox}}&WIDTH=256&HEIGHT=256"
            "&FORMAT=image/png&TRANSPARENT=TRUE&TIME={time}")


def wanted_style(now: float) -> str:
    return STYLE_WINTER if time.gmtime(now).tm_mon in WINTER_MONTHS else STYLE_SUMMER


def pick_style(styles: list[str], now: float) -> str:
    """The season's colour scale if the server lists it, else the other one, else the
    layer's default (empty)."""
    for style in (wanted_style(now), STYLE_WINTER, STYLE_SUMMER):
        if not styles or style in styles:
            return style
    return ""


def read_capabilities(xml: str, layer: str = LAYER) -> tuple[list[int], list[str]]:
    """The layer's newest FRAMES times and its style names, from a WMS 1.3.0 capabilities
    document (the layer's own, or one listing many layers)."""
    m = re.search(_NAME.format(ns=NAMESPACE, layer=re.escape(layer)), xml)
    if not m:
        return [], []
    end = len(xml)
    for stop in ("<Layer", "</Layer>"):  # the layer's block ends at the next layer or its end
        i = xml.find(stop, m.end())
        if 0 <= i < end:
            end = i
    block = xml[m.end():end]
    dim = _TIME.search(block)
    times = parse_time_dimension(dim.group(1), limit=FRAMES) if dim and dim.group(1).strip() else []
    return times, _STYLE.findall(block)


async def _capabilities(session: aiohttp.ClientSession) -> tuple[list[int], list[str]]:
    params = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetCapabilities"}
    err: Exception | None = None
    for url in CAPS_URLS:
        try:
            async with session.get(url, params=params, headers={"User-Agent": USER_AGENT},
                                   timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
                resp.raise_for_status()
                found = read_capabilities(await resp.text())
            if found[0]:
                return found
        except Exception as e:  # noqa: BLE001 - the next address, or the assumed times
            err = e
    _LOGGER.debug("Rain radar: no times in GetCapabilities (%s), assuming 5-minute frames", err)
    return [], []


# --- the next two hours: where the rain is heading ------------------------------------------
# FMI's open data has no radar forecast, so one is made here the way simple nowcasts are:
# find how the rain moved over the last quarter of an hour, and keep moving the newest
# picture the same way. Showers that form or die are not foreseen, so the frames blur and
# fade the further ahead they are. Good for "will that band reach us within the hour".

AHEAD = 24  # forecast frames: two hours in 5-minute steps
BASE_Z = 6  # the zoom the pictures are read at (about 1.2 km a pixel at 60 N)
MOTION_SPAN = 3  # frames between the two pictures the movement is read from (15 min)
SEARCH = 10  # the movement looked for, in quarter-size pixels each way (about 190 km/h)
BLOCKS = (4, 3)  # the picture is split so rain on a front's two sides can move differently
MIN_RAIN = 0.004  # a block with less of it wet than this goes by the whole picture's movement
FADE_BY = 3 * 3600  # a forecast frame's strength falls to FADE_MIN over this long
FADE_MIN = 0.35
KEEP_TILES = 5400  # s a forecast tile on disk is good for


def _tile_range(bounds: tuple[float, float, float, float], z: int) -> tuple[int, int, int, int]:
    import math
    s, w, n, e = bounds
    def tx(lon: float) -> int:
        return int((lon + 180) / 360 * 2**z)
    def ty(lat: float) -> int:
        r = math.radians(lat)
        return int((1 - math.asinh(math.tan(r)) / math.pi) / 2 * 2**z)
    return tx(w), ty(n), tx(e), ty(s)


def _score(a: Any, b: Any, dx: int, dy: int) -> float:
    """How unlike picture a is from picture b moved by (dx, dy); less is a better match."""
    from PIL import Image, ImageChops, ImageStat
    moved = Image.new("L", a.size)
    moved.paste(b, (dx, dy))
    return ImageStat.Stat(ImageChops.difference(a, moved)).sum[0]


def _best(a: Any, b: Any, around: tuple[int, int], reach: int) -> tuple[int, int]:
    best, at = None, around
    for dy in range(around[1] - reach, around[1] + reach + 1):
        for dx in range(around[0] - reach, around[0] + reach + 1):
            # A small pull towards the middle of the search, so empty pictures say "no movement known".
            sc = _score(a, b, dx, dy) + 0.5 * (abs(dx - around[0]) + abs(dy - around[1]))
            if best is None or sc < best:
                best, at = sc, (dx, dy)
    return at


def motion(new: Any, old: Any) -> tuple[tuple[int, int], list[list[tuple[float, float]]]]:
    """How the rain moved from `old` to `new` (two "L" pictures of where it rains), in pixels:
    the whole picture's movement, and one per block (BLOCKS across and down), each pulled
    halfway to the whole so neighbours don't tear apart."""
    from PIL import Image, ImageStat
    w, h = new.size
    small = (max(1, w // 4), max(1, h // 4))
    cx, cy = _best(new.resize(small, Image.BOX), old.resize(small, Image.BOX), (0, 0), SEARCH)
    whole = _best(new.resize((w // 2, h // 2), Image.BOX), old.resize((w // 2, h // 2), Image.BOX), (cx * 2, cy * 2), 1)
    whole = (whole[0] * 2, whole[1] * 2)
    field = []
    bw, bh = w // BLOCKS[0], h // BLOCKS[1]
    for j in range(BLOCKS[1]):
        row = []
        for i in range(BLOCKS[0]):
            box = (i * bw, j * bh, (i + 1) * bw, (j + 1) * bh)
            a, b = new.crop(box), old.crop(box)
            wet = ImageStat.Stat(a).sum[0] / 255 / (bw * bh)
            if wet < MIN_RAIN:
                row.append((float(whole[0]), float(whole[1])))
                continue
            half = (bw // 2, bh // 2)
            got = _best(a.resize(half, Image.BOX), b.resize(half, Image.BOX), (whole[0] // 2, whole[1] // 2), 3)
            row.append(((got[0] * 2 + whole[0]) / 2, (got[1] * 2 + whole[1]) / 2))
        field.append(row)
    return whole, field


def movement_at(field: list[list[tuple[float, float]]], fx: float, fy: float) -> tuple[float, float]:
    """The movement at a place in the picture (0..1 across and down), blended between blocks."""
    nx, ny = len(field[0]), len(field)
    gx = min(max(fx * nx - 0.5, 0.0), nx - 1.0)
    gy = min(max(fy * ny - 0.5, 0.0), ny - 1.0)
    x0, y0 = int(gx), int(gy)
    x1, y1 = min(nx - 1, x0 + 1), min(ny - 1, y0 + 1)
    tx, ty = gx - x0, gy - y0
    out = []
    for k in (0, 1):
        top = field[y0][x0][k] * (1 - tx) + field[y0][x1][k] * tx
        bottom = field[y1][x0][k] * (1 - tx) + field[y1][x1][k] * tx
        out.append(top * (1 - ty) + bottom * ty)
    return out[0], out[1]


def forecast_tile(picture: Any, origin: tuple[int, int], field: list[list[tuple[float, float]]], span: float,
                  lead: float, z: int, x: int, y: int) -> bytes | None:
    """One tile of the newest picture moved on by `lead` seconds. `picture` is RGBA at BASE_Z
    with its top-left tile at `origin`; `field` its movement over `span` seconds."""
    import io
    from PIL import Image, ImageFilter
    k = 2.0 ** (BASE_Z - z)
    left = (x * k - origin[0]) * 256
    top = (y * k - origin[1]) * 256
    size = 256 * k
    w, h = picture.size
    mx, my = movement_at(field, (left + size / 2) / w, (top + size / 2) / h)
    dx, dy = mx * lead / span, my * lead / span
    box = (left - dx, top - dy, left - dx + size, top - dy + size)
    if box[2] <= 0 or box[3] <= 0 or box[0] >= w or box[1] >= h:
        return None
    tile = picture.transform((256, 256), Image.EXTENT, box, Image.BILINEAR)
    alpha = tile.getchannel("A")
    if not alpha.getbbox():
        return None
    blur = min(4.0, lead / 1800 / max(k, 0.25))
    if blur >= 0.5:
        tile = tile.filter(ImageFilter.GaussianBlur(blur))
        alpha = tile.getchannel("A")
    strength = max(FADE_MIN, 1 - (1 - FADE_MIN) * lead / FADE_BY)
    tile.putalpha(alpha.point(lambda a: int(a * strength)))
    out = io.BytesIO()
    tile.save(out, "PNG", optimize=False)
    return out.getvalue()


class Nowcast:
    """Keeps the newest radar picture and its movement, and draws the forecast tiles."""

    def __init__(self, overlays: Overlays, overlay: Overlay):
        self.overlays, self.o = overlays, overlay
        self.base: int | None = None  # the newest real frame
        self.observed: list[int] = []
        self._state: tuple[int, Any, tuple[int, int], Any, float] | None = None  # base, picture, origin, field, span
        self._lock = asyncio.Lock()
        self.error: str | None = None
        self.moved: tuple[float, float] | None = None  # km/h east and north, for the log and tests
        self._used = 0.0

    def extend(self, observed: list[int]) -> list[int]:
        if self._state and time.time() - self._used > 900:
            self._state = None  # nobody has looked for a while: the picture is let go
        self.observed = sorted(int(t) for t in observed)
        if len(self.observed) < 2:
            self.base = None
            return self.observed
        self.base = self.observed[-1]
        return self.observed + [self.base + i * STEP for i in range(1, AHEAD + 1)]

    async def _mosaic(self, ts: int) -> tuple[Any, tuple[int, int]]:
        from PIL import Image
        x0, y0, x1, y1 = _tile_range(self.o.bounds or BOUNDS, BASE_Z)
        coords = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
        paths = await asyncio.gather(*(self.overlays.cache.get(f"{self.o.id}/{ts}", BASE_Z, x, y,
                                                               self.o.tile_url(ts, BASE_Z, x, y)) for x, y in coords))

        def build() -> Any:
            img = Image.new("RGBA", ((x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256))
            for (x, y), path in zip(coords, paths):
                if path:
                    with Image.open(path) as t:
                        img.paste(t.convert("RGBA"), ((x - x0) * 256, (y - y0) * 256))
            return img
        return await asyncio.to_thread(build), (x0, y0)

    async def _ready(self) -> tuple[int, Any, tuple[int, int], Any, float] | None:
        base = self.base
        if base is None:
            return None
        if self._state and self._state[0] == base:
            return self._state
        async with self._lock:
            if self._state and self._state[0] == base:
                return self._state
            earlier = [t for t in self.observed if t <= base - MOTION_SPAN * STEP] or self.observed[:1]
            old_ts = earlier[-1]
            (new, origin), (old, _) = await asyncio.gather(self._mosaic(base), self._mosaic(old_ts))
            whole, field = await asyncio.to_thread(motion, new.getchannel("A"), old.getchannel("A"))
            span = float(base - old_ts)
            import math
            x0, y0, x1, y1 = _tile_range(self.o.bounds or BOUNDS, BASE_Z)
            km_px = 40075.0 * math.cos(math.radians(62)) / (2**BASE_Z * 256)
            self.moved = (whole[0] * km_px * 3600 / span, -whole[1] * km_px * 3600 / span)
            _LOGGER.debug("Rain radar forecast from %s: moving %.0f km/h east, %.0f km/h north", base, *self.moved)
            self._state = (base, new, origin, field, span)
            return self._state

    async def tile(self, ts: int, z: int, x: int, y: int) -> Path | None:
        state = await self._ready()
        if not state:
            return None
        base, picture, origin, field, span = state
        self._used = time.time()
        # ".partial" so the browser doesn't keep it: the same time gets a better forecast,
        # and then the real frame, a few minutes later.
        path = self.overlays.cache.dir / self.o.id / f"fc{base}" / str(ts) / str(z) / str(x) / f"{y}.partial.png"
        if path.exists():
            return path if path.stat().st_size else None
        body = await asyncio.to_thread(forecast_tile, picture, origin, field, span, float(ts - base), z, x, y)
        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body or b"")
        await asyncio.to_thread(write)
        return path if body else None


def overlay(**changes: Any) -> Overlay:
    kw: dict[str, Any] = dict(
        id=ID, title="Rain radar", kind="tiles", url=tile_url(STYLE_WINTER), times=_fallback,
        max_age=6 * 3600, refresh=STEP, frame_ms=500, opacity=0.7, color="#2563eb",
        min_zoom=4, max_zoom=8, bounds=BOUNDS, valid_for=2 * STEP, attribution=ATTRIBUTION,
        note="FMI's radar for the last hour in 5-minute frames, then two hours ahead: the newest picture moved on "
             "the way the rain has been moving, fainter the further ahead. Showers that form or die aren't "
             "foreseen. Press play on the time bar.",
    )
    kw.update(changes)
    o = Overlay(**kw)
    if "times" not in changes:
        async def list_times(session: aiohttp.ClientSession | None, now: float) -> list[float]:
            if session is not None:
                times, styles = await _capabilities(session)
                if times:
                    o.url = tile_url(pick_style(styles, now))  # the next frames fetched use it
                    return [t for t in times if t <= now]
            o.url = tile_url(wanted_style(now))
            return await _fallback(session, now)
        o.times = list_times
    return o


def register(overlays: Overlays, **changes: Any) -> Nowcast:
    o = overlay(**changes)
    nowcast = Nowcast(overlays, o)
    observed = o.times

    async def times(session: aiohttp.ClientSession | None, now: float) -> list[float]:
        return nowcast.extend([int(t) for t in await observed(session, now)])

    o.times = times
    o.future = nowcast.tile
    o.future_from = lambda: nowcast.base
    overlays.register(o)
    return nowcast
