"""FMI's daily ice chart as a map overlay, drawn here in the ice chart's own colours.

The Finnish Ice Service's analysts draw the Baltic ice chart each day of the ice season
(the PDF on FMI's ice conditions page). The same chart, as numbers on a 1 km grid, is the
Copernicus Marine product SEAICE_BAL_SEAICE_L4_NRT_OBSERVATIONS_011_004: ice concentration
(%) and ice thickness (cm). Neither needs an account through the open WMTS, which serves
both as pictures. They are read here as grey pictures with a fixed range (black the lowest
value, white the highest), 25 tiles a layer at zoom 6 covering 53-66 N, 9-31 E, which turns
them back into numbers (calibrated against a few of the service's own point readings), and
kept per day as two small greyscale PNGs.

Map tiles are drawn from those with Pillow: the drift ice classes in the WMO colours the
ice chart uses (open water, very open, open, close and very close drift ice, and solid ice:
fast or consolidated), the thickness in cm written on the ice, and everything cut to the
water of the drawn chart (as the sea layers are) so the 1 km grid doesn't run over the
shore. A tap reads the class, concentration and thickness.

The days are the service's own (from its time dimension); the panel shows the newest, and
any earlier day of this or last winter can be picked, which is also how it can be looked at
before the ice comes.
"""

from __future__ import annotations

import asyncio
import calendar
import io
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from .config import USER_AGENT
from .ice import CHART_URL
from .overlays import Overlay, Overlays, iso, parse_time_dimension, tile_bounds
from .seaice import times_from_capabilities

_LOGGER = logging.getLogger(__name__)

ID = "ice-chart"
PRODUCT = "SEAICE_BAL_SEAICE_L4_NRT_OBSERVATIONS_011_004"
CONC_LAYER = f"{PRODUCT}/FMI-BAL-SEAICE_CONC-L4-NRT-OBS/ice_concentration"
THICK_LAYER = f"{PRODUCT}/FMI-BAL-SEAICE_THICK-L4-NRT-OBS/ice_thickness"
ATTRIBUTION = "Ice chart: Finnish Ice Service (FMI) via E.U. Copernicus Marine Service"

# The grid: zoom-6 web mercator tiles over the product's area (53-66 N, 9-31 E), about
# 1.2 km a pixel at 60 N, as fine as the product's own 1 km.
GRID_Z = 6
COLS = range(33, 38)
ROWS = range(16, 21)
TILE = 256
GRID_W, GRID_H = TILE * len(COLS), TILE * len(ROWS)
X0, Y0 = COLS[0] * TILE, ROWS[0] * TILE  # the grid's corner in zoom-6 world pixels
NONE = 255  # no value (land, or outside the product)

# Each layer as grey: (layer, top of the range). Concentration 0-100 %; thickness 0-200 cm
# (the Bothnian Bay's fast ice reaches about 120).
LAYERS = {"conc": (CONC_LAYER, 100.0), "thick": (THICK_LAYER, 200.0)}
GREY = "cmap:gray,range:0/{top:g},noClamp"
TILE_URL = (CHART_URL + "?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER={layer}&STYLE={style}"
            "&TILEMATRIXSET=EPSG:3857&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/png&TIME={time}")
INFO_URL = (CHART_URL + "?SERVICE=WMTS&REQUEST=GetFeatureInfo&VERSION=1.0.0&LAYER={layer}&STYLE={style}"
            "&TILEMATRIXSET=EPSG:3857&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&I={i}&J={j}"
            "&INFOFORMAT=application/json&TIME={time}")
CALIBRATE_N = 8  # point readings a day's grey is checked against
TIMEOUT = 30
PARALLEL = 4
KEEP_DAYS = 40  # day grids kept on disk (two small PNGs each), the newest used first

# Drift ice classes as on the ice chart (WMO colour code for total concentration):
# (from %, name, short, RGB). Below the first, open water: not coloured.
CLASSES = [
    (1, "Open water (under 1/10 ice)", "<1/10", (150, 200, 255)),
    (10, "Very open drift ice (1-3/10)", "1-3/10", (140, 255, 160)),
    (35, "Open drift ice (4-6/10)", "4-6/10", (255, 255, 0)),
    (65, "Close drift ice (7-8/10)", "7-8/10", (255, 125, 7)),
    (85, "Very close drift ice (9-10/10)", "9-10/10", (255, 0, 0)),
    (99.5, "Fast or consolidated ice (10/10)", "10/10", (150, 150, 150)),
]
ALPHA = 235
EDGE_ALPHA = 150
LABEL_Z = 7  # thickness numbers from this zoom
LABEL_STEP = 160  # world pixels between them (at every zoom, so they stay apart on screen)
OUT = 512  # pixels a side of a drawn tile (sharp on phones; shown at 256 CSS px)

PENDING = object()  # the chart's water for a tile isn't there yet

Get = Callable[[str], Awaitable[bytes]]
Water = Callable[[int, int, int], Awaitable[tuple[dict[str, Any], bool] | None]]


# --- days ---------------------------------------------------------------------------------

def season_start(ts: float) -> int:
    """1 November of the ice season `ts` is in (or the one just over, June to October)."""
    t = time.gmtime(ts)
    year = t.tm_year if t.tm_mon >= 11 else t.tm_year - 1
    return calendar.timegm((year, 11, 1, 0, 0, 0, 0, 0, 0))


def last_season(ts: float) -> tuple[int, int]:
    """The previous ice season, 1 November to 31 May."""
    start = season_start(season_start(ts) - 86400)
    end_year = time.gmtime(start).tm_year + 1
    return start, calendar.timegm((end_year, 6, 1, 0, 0, 0, 0, 0, 0))


def in_season(ts: float) -> bool:
    return time.gmtime(ts).tm_mon in (11, 12, 1, 2, 3, 4, 5)


def keep_days(days: list[int], now: float) -> list[int]:
    """This winter's and last winter's days (November to May) up to now."""
    start = last_season(now)[0]
    return [d for d in days if start <= d <= now and in_season(d)]


def fallback_days(now: float) -> list[int]:
    """Every day of this and last winter until yesterday, if the service can't list its own."""
    last = int(now // 86400 * 86400) - 86400
    return keep_days(list(range(last_season(now)[0], last + 1, 86400)), now)


def peak_day(days: list[int], now: float) -> int | None:
    """Last winter's usual widest ice (late February), as near as the days have it."""
    start = last_season(now)[0]
    year = time.gmtime(start).tm_year + 1
    want = calendar.timegm((year, 2, 25, 0, 0, 0, 0, 0, 0))
    old = [d for d in days if d < season_start(now)]
    return min(old, key=lambda d: abs(d - want)) if old else None


# --- grey to numbers ----------------------------------------------------------------------

def _lstar(g: int) -> float:
    """CIE lightness (0-1) of an sRGB grey: cmocean's "gray" runs evenly in lightness."""
    c = g / 255
    lin = c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    f = lin ** (1 / 3) if lin > 216 / 24389 else (24389 / 27 * lin + 16) / 116
    return max(0.0, min(1.0, (116 * f - 16) / 100))


MODELS: dict[str, Callable[[int], float]] = {"lightness": _lstar, "linear": lambda g: g / 255}


def lut(model: str, top: float, scale: float = 1.0) -> list[int]:
    """Grey level -> stored value (0-254), by a model of how the grey was made."""
    fn = MODELS.get(model, _lstar)
    return [min(254, max(0, round(fn(g) * top * scale))) for g in range(256)]


def pick_model(pairs: list[tuple[int, float]], top: float) -> str:
    """The grey model closest to the service's own readings: [(grey, value)]."""
    if not pairs:
        return "lightness"
    def err(name: str) -> float:
        return sum((MODELS[name](g) * top - v) ** 2 for g, v in pairs)
    return min(MODELS, key=err)


# --- the day's grid -----------------------------------------------------------------------

def decode_tiles(tiles: dict[tuple[int, int], bytes | None], table: list[int]) -> bytes:
    """Grey tiles (col, row) -> one L grid of stored values, NONE where see-through."""
    from PIL import Image

    grid = Image.new("L", (GRID_W, GRID_H), NONE)
    for (col, row), body in tiles.items():
        if not body:
            continue
        img = Image.open(io.BytesIO(body))
        img = img.convert("LA") if img.mode not in ("LA", "L") else img
        if img.mode == "L":
            grey, alpha = img, Image.new("L", img.size, 255)
        else:
            grey, alpha = img.split()
        values = grey.point(table)
        none = Image.new("L", img.size, NONE)
        mask = alpha.point(lambda a: 255 if a >= 128 else 0)
        none.paste(values, (0, 0), mask)
        grid.paste(none, ((col - COLS[0]) * TILE, (row - ROWS[0]) * TILE))
    return _png(grid)


def _png(img: Any) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG", compress_level=6)
    return out.getvalue()


def stats(conc_png: bytes, thick_png: bytes) -> dict[str, Any]:
    """How much ice the day has: pixels with any ice, and the thickest."""
    from PIL import Image

    conc = Image.open(io.BytesIO(conc_png))
    hist = conc.histogram()
    ice = sum(hist[1:NONE])
    sea = sum(hist[:NONE])
    thick = Image.open(io.BytesIO(thick_png))
    th = thick.histogram()
    top = max((v for v in range(NONE) if th[v]), default=0)
    return {"ice_px": ice, "sea_px": sea, "share": round(ice / sea, 4) if sea else 0, "max_cm": top}


class Grids:
    """Day grids on disk: <dir>/<YYYY-MM-DD>/conc.png, thick.png, meta.json."""

    def __init__(self, directory: Path, session: aiohttp.ClientSession | None, get: Get | None = None):
        self.dir = directory
        self.session = session
        self.get = get or self._get
        self._locks: dict[int, asyncio.Lock] = {}
        self._open: dict[int, tuple[Any, Any, dict[str, Any]]] = {}  # day -> (conc, thick, meta), last two
        self.models: dict[str, str] = {}
        self.failed: dict[int, float] = {}

    async def _get(self, url: str) -> bytes:
        if self.session is None:
            raise ValueError("no connection")
        async with self.session.get(url, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            if resp.status in (204, 404):
                return b""
            resp.raise_for_status()
            return await resp.read()

    def _path(self, day: int) -> Path:
        return self.dir / iso(day)[:10]

    def meta(self, day: int) -> dict[str, Any] | None:
        try:
            return json.loads((self._path(day) / "meta.json").read_text())
        except (OSError, ValueError):
            return None

    async def ready(self, day: int) -> tuple[Any, Any, dict[str, Any]] | None:
        """The day's grids (PIL images), read from the service the first time."""
        hit = self._open.get(day)
        if hit:
            return hit
        lock = self._locks.setdefault(day, asyncio.Lock())
        async with lock:
            hit = self._open.get(day)
            if hit:
                return hit
            path = self._path(day)
            if not (path / "meta.json").exists():
                if time.time() - self.failed.get(day, 0) < 600:
                    return None
                try:
                    await self._build(day)
                except Exception:
                    self.failed[day] = time.time()
                    raise
            loaded = await asyncio.to_thread(_load, path)
            if loaded is None:
                return None
            self._open[day] = loaded
            while len(self._open) > 2:
                self._open.pop(next(iter(self._open)))
            return loaded

    async def _tiles(self, name: str, day: int) -> dict[tuple[int, int], bytes | None]:
        layer, top = LAYERS[name]
        sem = asyncio.Semaphore(PARALLEL)
        style = GREY.format(top=top)

        async def one(col: int, row: int) -> tuple[tuple[int, int], bytes | None]:
            url = TILE_URL.format(layer=layer, style=style, z=GRID_Z, x=col, y=row, time=iso(day))
            async with sem:
                try:
                    return (col, row), await self.get(url)
                except Exception as err:  # noqa: BLE001 - one missing tile leaves a hole, no more
                    _LOGGER.debug("Ice chart tile %s %d/%d failed: %s", name, col, row, err)
                    return (col, row), None
        got = await asyncio.gather(*(one(c, r) for r in ROWS for c in COLS))
        return dict(got)

    async def _calibrate(self, name: str, day: int, tiles: dict[tuple[int, int], bytes | None]) -> str:
        """Which grey model fits: a few pixels with different greys, read as numbers from
        the service, compared with what each model makes of them. Kept once found."""
        if name in self.models:
            return self.models[name]
        layer, top = LAYERS[name]
        picks = await asyncio.to_thread(_calibration_pixels, tiles, CALIBRATE_N)
        pairs: list[tuple[int, float]] = []
        for (col, row, i, j, grey) in picks:
            url = INFO_URL.format(layer=layer, style=GREY.format(top=top), z=GRID_Z, x=col, y=row, i=i, j=j,
                                  time=iso(day))
            try:
                value = parse_info(await self.get(url))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Ice chart reading failed: %s", err)
                continue
            if value is not None:
                pairs.append((grey, value))
        if len(pairs) < 3:
            return "lightness"  # not settled: asked again with the next day
        model = pick_model(pairs, top)
        self.models[name] = model
        _LOGGER.info("Ice chart %s: grey read as %s (%d readings)", name, model, len(pairs))
        return model

    async def _build(self, day: int) -> None:
        conc_tiles = await self._tiles("conc", day)
        if not any(conc_tiles.values()):
            raise ValueError(f"no ice chart for {iso(day)[:10]}")
        thick_tiles = await self._tiles("thick", day)
        out: dict[str, bytes] = {}
        for name, tiles in (("conc", conc_tiles), ("thick", thick_tiles)):
            model = await self._calibrate(name, day, tiles)
            out[name] = await asyncio.to_thread(decode_tiles, tiles, lut(model, LAYERS[name][1]))
        meta = {"day": iso(day)[:10], "made": int(time.time()), **await asyncio.to_thread(stats, out["conc"], out["thick"])}
        path = self._path(day)
        tmp = path.with_name(path.name + ".tmp")
        await asyncio.to_thread(_write_day, tmp, path, out, meta)
        await asyncio.to_thread(self.prune)
        _LOGGER.info("Ice chart for %s read: ice on %.1f%% of the sea, thickest %d cm",
                     meta["day"], 100 * meta["share"], meta["max_cm"])

    def prune(self) -> None:
        try:
            days = sorted(p for p in self.dir.iterdir() if p.is_dir() and not p.name.endswith(".tmp"))
        except OSError:
            return
        for old in days[:-KEEP_DAYS]:
            for f in old.iterdir():
                f.unlink(missing_ok=True)
            old.rmdir()


def _write_day(tmp: Path, path: Path, out: dict[str, bytes], meta: dict[str, Any]) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    for name, body in out.items():
        (tmp / f"{name}.png").write_bytes(body)
    (tmp / "meta.json").write_text(json.dumps(meta))
    if path.exists():
        for f in path.iterdir():
            f.unlink()
        path.rmdir()
    os.replace(tmp, path)


def _load(path: Path) -> tuple[Any, Any, dict[str, Any]] | None:
    from PIL import Image

    try:
        conc = Image.open(path / "conc.png")
        conc.load()
        thick = Image.open(path / "thick.png")
        thick.load()
        meta = json.loads((path / "meta.json").read_text())
    except (OSError, ValueError):
        return None
    return conc, thick, meta


def _calibration_pixels(tiles: dict[tuple[int, int], bytes | None], n: int) -> list[tuple[int, int, int, int, int]]:
    """Up to n pixels (col, row, i, j, grey) spread over the greys the tiles have."""
    from PIL import Image

    seen: dict[int, tuple[int, int, int, int, int]] = {}
    for (col, row), body in sorted(tiles.items()):
        if not body:
            continue
        img = Image.open(io.BytesIO(body)).convert("LA")
        px = img.load()
        for j in range(8, TILE, 16):
            for i in range(8, TILE, 16):
                g, a = px[i, j]
                if a >= 250 and g // 16 not in seen:
                    seen[g // 16] = (col, row, i, j, g)
    picks = sorted(seen.values(), key=lambda p: p[4])
    if len(picks) <= n:
        return picks
    step = (len(picks) - 1) / (n - 1)
    return [picks[round(k * step)] for k in range(n)]


def parse_info(body: bytes) -> float | None:
    """The number in a GetFeatureInfo answer (JSON): the first feature's value."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    feats = data.get("features") if isinstance(data, dict) else None
    if not feats:
        return None
    props = feats[0].get("properties") or {}
    for key in ("value", "Value", "val"):
        if isinstance(props.get(key), (int, float)):
            return float(props[key])
    for v in props.values():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


# --- drawing ------------------------------------------------------------------------------

def class_of(conc: float) -> int | None:
    """Index into CLASSES, None for open water."""
    found = None
    for i, (lo, *_rest) in enumerate(CLASSES):
        if conc >= lo:
            found = i
    return found


def _palette() -> tuple[list[int], list[int]]:
    """Concentration (0-254) -> palette index, and the palette (index 0 see-through)."""
    pal = [0, 0, 0]
    for *_, rgb in CLASSES:
        pal += list(rgb)
    index = []
    for v in range(256):
        c = class_of(v) if v != NONE else None
        index.append(0 if c is None else c + 1)
    return index, pal


def _world_box(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """The tile's box in the grid's pixels."""
    k = 2 ** (GRID_Z - z)
    return (x * TILE * k - X0, y * TILE * k - Y0, (x + 1) * TILE * k - X0, (y + 1) * TILE * k - Y0)


def water_mask(water: dict[str, Any], size: int, crop: tuple[float, float, float] = (0, 0, 1)) -> Any:
    """The water of a chart tile (vchart.water_of) as an L mask, 255 on the water.
    `crop` (x, y, k) takes the part of a lower zoom's tile at offset x, y (0-1) of size k."""
    from PIL import Image, ImageChops, ImageDraw

    e = water.get("e") or 512
    ox, oy, k = crop
    scale = size / (e * k)

    def pts(ring: list[float]) -> list[tuple[float, float]]:
        return [((ring[i] - ox * e) * scale, (ring[i + 1] - oy * e) * scale) for i in range(0, len(ring) - 1, 2)]

    def fill(groups: list[list[list[float]]]) -> Any:
        out = Image.new("L", (size, size), 0)
        for rings in groups:
            if not rings:
                continue
            part = Image.new("1", (size, size), 0)
            for ring in rings:
                if len(ring) < 6:
                    continue
                one = Image.new("1", (size, size), 0)
                ImageDraw.Draw(one).polygon(pts(ring), fill=1)
                part = ImageChops.logical_xor(part, one)  # even-odd, as the panel fills them
            out = ImageChops.lighter(out, part.convert("L"))
        return out

    mask = fill(water.get("water") or [])
    lakes = water.get("lake") or []
    if lakes:
        mask = ImageChops.subtract(mask, fill(lakes))
    return mask


_FONT: dict[int, Any] = {}


def _font(px: int) -> Any:
    from PIL import ImageFont

    if px not in _FONT:
        try:
            _FONT[px] = ImageFont.load_default(size=px)
        except Exception:  # noqa: BLE001 - Pillow without FreeType: its small bitmap font
            _FONT[px] = ImageFont.load_default()
    return _FONT[px]


def draw_tile(conc: Any, thick: Any, z: int, x: int, y: int, water: Any | None, size: int = OUT) -> bytes | None:
    """One map tile of a day's chart; `water` an L mask of the tile's water, or None."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    bx0, by0, bx1, by1 = _world_box(z, x, y)
    if bx1 <= 0 or by1 <= 0 or bx0 >= GRID_W or by0 >= GRID_H:
        return None
    data = (bx0, by0, bx1, by1)
    # Smooth between the 1 km cells: concentration with "none" as 0 and a mask of where
    # there are values, both stretched bilinearly.
    c0 = conc.point([0 if v == NONE else v for v in range(256)])
    valid = conc.point([0 if v == NONE else 255 for v in range(256)])
    c = c0.transform((size, size), Image.Transform.EXTENT, data, Image.Resampling.BILINEAR, fillcolor=0)
    cell = size / ((bx1 - bx0) or 1)  # output pixels per grid cell
    if cell > 2:
        # Zoomed in, the 1 km cells would show as steps along the class lines: smoothed
        # over about half a cell (from a margin around the tile, so tiles still meet).
        pad = (bx1 - bx0) * 0.25
        big = c0.transform((size * 3 // 2, size * 3 // 2), Image.Transform.EXTENT,
                           (bx0 - pad, by0 - pad, bx1 + pad, by1 + pad), Image.Resampling.BILINEAR, fillcolor=0)
        big = big.filter(ImageFilter.GaussianBlur(min(cell * 0.8, 80)))
        m = size // 4
        c = big.crop((m, m, m + size, m + size))
    ok = valid.transform((size, size), Image.Transform.EXTENT, data, Image.Resampling.BILINEAR, fillcolor=0)
    index, pal = _palette()
    cls = c.point(index)
    if not cls.getbbox():
        return None
    painted = cls.point([0] + [ALPHA] * 255)
    alpha = ImageChops.multiply(painted, ok.point(lambda v: 255 if v >= 128 else 0))
    if water is not None:
        alpha = ImageChops.multiply(alpha, water)
    if not alpha.getbbox():
        return None
    img = cls.copy()
    img.putpalette(pal)
    img = img.convert("RGBA")
    img.putalpha(alpha)
    # Thin dark lines between the classes, as the chart's analysts draw their areas.
    edges = cls.convert("L").filter(ImageFilter.FIND_EDGES).point(lambda v: EDGE_ALPHA if v else 0)
    ImageDraw.Draw(edges).rectangle((0, 0, size - 1, size - 1), outline=0)  # the filter's own edge
    edges = ImageChops.multiply(edges, alpha.point(lambda v: 255 if v else 0))
    if edges.getbbox():
        img.alpha_composite(Image.merge("RGBA", (*[Image.new("L", img.size, 40)] * 3, edges)))
    if z >= LABEL_Z:
        _labels(img, conc, thick, z, x, y, water, size)
    return _png(img)


def _labels(img: Any, conc: Any, thick: Any, z: int, x: int, y: int, water: Any | None, size: int) -> None:
    """Thickness (cm) on the ice, on a lattice fixed to the world so tiles agree."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    font = _font(max(11, size // 24))
    k = size / TILE
    world0x, world0y = x * TILE, y * TILE
    first_x = math.ceil((world0x - LABEL_STEP / 2) / LABEL_STEP) * LABEL_STEP + LABEL_STEP / 2
    first_y = math.ceil((world0y - LABEL_STEP / 2) / LABEL_STEP) * LABEL_STEP + LABEL_STEP / 2
    gk = 2 ** (GRID_Z - z)
    margin = 18
    wy = first_y
    while wy < world0y + TILE:
        wx = first_x
        while wx < world0x + TILE:
            px, py = (wx - world0x) * k, (wy - world0y) * k
            if margin * k / 2 <= px <= size - margin * k / 2 and margin * k / 4 <= py <= size - margin * k / 4:
                gx, gy = int(wx * gk - X0), int(wy * gk - Y0)
                if 0 <= gx < GRID_W and 0 <= gy < GRID_H:
                    cv, tv = conc.getpixel((gx, gy)), thick.getpixel((gx, gy))
                    wet = water is None or water.getpixel((min(size - 1, int(px)), min(size - 1, int(py)))) >= 128
                    if cv != NONE and cv >= CLASSES[1][0] and tv != NONE and tv > 0 and wet:
                        draw.text((px, py), str(tv), font=font, fill=(17, 24, 39, 255), anchor="mm",
                                  stroke_width=max(2, size // 200), stroke_fill=(255, 255, 255, 230))
            wx += LABEL_STEP
        wy += LABEL_STEP


def value_at(conc: Any, thick: Any, lat: float, lon: float) -> tuple[int, int] | None:
    """Stored concentration and thickness at a place, None outside or on land."""
    n = 2 ** GRID_Z * TILE
    wx = (lon + 180) / 360 * n
    s = math.sin(math.radians(lat))
    wy = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    gx, gy = int(wx - X0), int(wy - Y0)
    if not (0 <= gx < GRID_W and 0 <= gy < GRID_H):
        return None
    cv = conc.getpixel((gx, gy))
    if cv == NONE:
        return None
    tv = thick.getpixel((gx, gy))
    return cv, (0 if tv == NONE else tv)


# --- the overlay --------------------------------------------------------------------------

async def list_days(session: aiohttp.ClientSession | None, now: float, get: Get | None = None) -> list[int]:
    """The days the service has, of this and last winter."""
    try:
        if get is not None:
            xml = (await get(CHART_URL + "?SERVICE=WMTS&REQUEST=GetCapabilities&VERSION=1.0.0&layer=" + PRODUCT)).decode()
        elif session is not None:
            params = {"SERVICE": "WMTS", "REQUEST": "GetCapabilities", "VERSION": "1.0.0", "layer": PRODUCT}
            async with session.get(CHART_URL, params=params, headers={"User-Agent": USER_AGENT},
                                   timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
                resp.raise_for_status()
                xml = await resp.text()
        else:
            xml = ""
        found = keep_days([d // 86400 * 86400 for d in times_from_capabilities(xml, CONC_LAYER, limit=800)], now)
        if found:
            return sorted(set(found))
        _LOGGER.debug("Ice chart: no days in GetCapabilities, assuming daily")
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Ice chart GetCapabilities failed (%s), assuming daily", err)
    return fallback_days(now)


class IceChart:
    def __init__(self, directory: Path, session: aiohttp.ClientSession | None, water: Water | None = None,
                 get: Get | None = None):
        self.grids = Grids(directory, session, get)
        self.session = session
        self.water = water
        self.get = get
        self.days: list[int] = []
        self.overlay: Overlay | None = None
        self._reading: asyncio.Task | None = None

    async def times(self, session: Any, now: float) -> list[float]:
        self.days = await list_days(self.session, now, self.get)
        # The newest day is read straight away, so the panel can say whether it has ice
        # (and show last winter's instead if not).
        if self.days and self.grids.meta(self.days[-1]) is None and not self._reading:
            self._reading = asyncio.create_task(self._read_latest(self.days[-1]))
        return self.days

    async def _read_latest(self, day: int) -> None:
        try:
            await self.grids.ready(day)
        except Exception as err:  # noqa: BLE001 - shown again when a tile is asked for
            _LOGGER.warning("Could not read the ice chart for %s: %s", iso(day)[:10], err)
        finally:
            self._reading = None

    async def _water(self, z: int, x: int, y: int) -> tuple[Any | None, bool]:
        """The tile's water mask and whether it's final (the chart may still be downloading)."""
        if self.water is None or z < 5:
            return None, True
        wz = min(z, 12)
        k = 2 ** (z - wz)
        try:
            got = await self.water(wz, x // k, y // k)
        except Exception as err:  # noqa: BLE001 - no water: drawn uncut, asked again
            _LOGGER.debug("Ice chart: no water for %d/%d/%d: %s", z, x, y, err)
            return None, False
        if got is None:
            return PENDING, False
        water, complete = got
        if not water.get("water") and not complete:
            return None, False  # the chart has nothing here yet: drawn uncut for now
        crop = ((x % k) / k, (y % k) / k, 1 / k)
        return await asyncio.to_thread(water_mask, water, OUT, crop), complete

    async def tile(self, ts: int, z: int, x: int, y: int) -> tuple[bytes | None, bool]:
        s, w, n, e = tile_bounds(z, x, y)
        if n < 53 or s > 66 or e < 9 or w > 31:
            return None, True
        day = await self.grids.ready(ts)
        if day is None:
            return None, False
        conc, thick, meta = day
        if not meta.get("ice_px"):
            return None, True
        water, final = await self._water(z, x, y)
        if water is PENDING:
            return None, False  # the chart is still coming: the panel asks again shortly
        body = await asyncio.to_thread(draw_tile, conc, thick, z, x, y, water)
        return body, final

    async def value(self, lat: float, lon: float, ts: int) -> dict[str, Any] | None:
        day = self._day_for(ts)
        if day is None:
            return None
        got = await self.grids.ready(day)
        if got is None:
            return None
        conc, thick, meta = got
        v = value_at(conc, thick, lat, lon)
        if v is None:
            return None
        cv, tv = v
        c = class_of(cv)
        return {"day": meta.get("day"), "conc": cv, "thick": tv if c is not None else 0,
                "class": CLASSES[c][1] if c is not None else "No ice", "short": CLASSES[c][2] if c is not None else ""}

    def _day_for(self, ts: int) -> int | None:
        if not self.days:
            return None
        if ts in self.days:
            return ts
        older = [d for d in self.days if d <= ts]
        return older[-1] if older else self.days[0]

    def info(self, now: float) -> dict[str, Any]:
        """For the panel's day picker: the newest day and whether it has ice, last winter's
        usual peak, and the day to show first (the newest, or the peak while the newest
        has no ice)."""
        latest = self.days[-1] if self.days else None
        meta = self.grids.meta(latest) if latest else None
        has_ice = None if meta is None else bool(meta.get("ice_px"))
        peak = peak_day(self.days, now)
        default = peak if has_ice is False and peak else latest
        return {"latest": latest, "latest_ice": has_ice, "latest_share": meta.get("share") if meta else None,
                "max_cm": meta.get("max_cm") if meta else None, "peak": peak, "default": default,
                "season_start": season_start(now)}


def overlay(chart: IceChart, **changes: Any) -> Overlay:
    kw: dict[str, Any] = dict(
        id=ID, title="Ice chart", kind="local", times=chart.times, tile=chart.tile, value=chart.value,
        static=True, pickable=True, info=chart.info, max_age=400 * 86400, refresh=3 * 3600, color="#ef4444", icon="ice", opacity=0.8,
        min_zoom=4, max_zoom=12, bounds=(53.0, 9.0, 66.0, 31.0), valid_for=86400, attribution=ATTRIBUTION,
        note="FMI's daily ice chart: drift ice by how close it is (WMO colours), grey for fast ice, "
             "thickness in cm. Tap the ice for its class and thickness.",
    )
    kw.update(changes)
    o = Overlay(**kw)
    chart.overlay = o
    return o


def register(registry: Overlays, directory: Path, session: aiohttp.ClientSession | None,
             water: Water | None = None, get: Get | None = None, **changes: Any) -> IceChart:
    chart = IceChart(directory, session, water, get)
    registry.register(overlay(chart, **changes))
    return chart
