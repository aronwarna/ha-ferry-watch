"""Picture work for the historic maps (mapsources.py): what part of an old photo or map is
real picture, and tiles with the rest made see-through.

Old map services answer outside their pictures in three ways: see-through (good), plain
white (services that can't do see-through: the Espoo, Turku and Oulu servers), or black
borders baked into the photo mosaics themselves (the "collars" round each flight's
pictures). Both white and black then cover today's map as big boxes.

Two steps put that right:

1. **Coverage** (``footprint``): once per layer, a small picture of the layer's whole area
   is read, and everything joined to its edge through see-through, pure black or (for
   services without see-through) pure white is outside. What's left is kept as a coarse
   grid (``Cover``): which cells of the area have picture. The panel uses it to offer only
   the years that show where you are looking, and to draw their outline.

2. **Tiles** (``clean_tile``): near the coverage edge only (never in the middle of a
   picture, where a black shadow or a white roof is real), blobs of pure black or white at
   least 5 px across become see-through, with the dark seam JPEG leaves round them.

Everything is Pillow's own C filters, so a tile takes a few milliseconds and no numpy is
needed. ``Lest97`` turns web mercator into Estonia's own grid (EPSG:3301) for the
Estonian Land Board's old maps, which are served in nothing else.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageFilter

HALF = 20037508.342789244
BLACK = 10        # every channel at or below this is "pure black" (nodata collars are 0-3)
WHITE = 250       # every channel at or above this is "pure white" (a background, on opaque services)
HALO_DARK = 48    # the seam JPEG leaves round a black collar
HALO_LIGHT = 232
BLOB = 5          # pure black/white must be at least this many px across to go
GRID_LONG = 112   # coverage cells along the longer side of a layer's area
OVERVIEW_LONG = 512


# --- mercator ------------------------------------------------------------------------
def merc(lat: float, lon: float) -> tuple[float, float]:
    lat = max(-85.0, min(85.0, lat))
    return lon / 180 * HALF, math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * HALF / math.pi


def unmerc(x: float, y: float) -> tuple[float, float]:
    return math.degrees(2 * math.atan(math.exp(y / HALF * math.pi)) - math.pi / 2), x / HALF * 180


def box_merc(box) -> tuple[float, float, float, float]:
    """(south, west, north, east) in degrees -> (x0, y0, x1, y1) in mercator metres."""
    x0, y0 = merc(box[0], box[1])
    x1, y1 = merc(box[2], box[3])
    return x0, y0, x1, y1


def tile_merc(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = 2 * HALF / 2**z
    x0, y1 = -HALF + x * size, HALF - y * size
    return x0, y1 - size, x0 + size, y1


def overview_size(mbox: tuple[float, float, float, float], long: int = OVERVIEW_LONG) -> tuple[int, int]:
    w, h = mbox[2] - mbox[0], mbox[3] - mbox[1]
    if w >= h:
        return long, max(16, round(long * h / w))
    return max(16, round(long * w / h)), long


# --- Estonia's grid, L-EST97 (Lambert conformal conic on GRS80) ------------------------
class Lest97:
    A = 6378137.0
    F = 1 / 298.257222101
    LAT1, LAT2, LAT0, LON0 = 59 + 20 / 60, 58.0, 57.51755393055556, 24.0
    X0, Y0 = 500000.0, 6375000.0

    def __init__(self) -> None:
        a, f = self.A, self.F
        self.e = e = math.sqrt(f * (2 - f))
        p1, p2, p0 = (math.radians(v) for v in (self.LAT1, self.LAT2, self.LAT0))
        m = lambda p: math.cos(p) / math.sqrt(1 - (e * math.sin(p)) ** 2)  # noqa: E731
        t = lambda p: math.tan(math.pi / 4 - p / 2) / ((1 - e * math.sin(p)) / (1 + e * math.sin(p))) ** (e / 2)  # noqa: E731
        self.n = (math.log(m(p1)) - math.log(m(p2))) / (math.log(t(p1)) - math.log(t(p2)))
        self.F_ = m(p1) / (self.n * t(p1) ** self.n)
        self.r0 = a * self.F_ * t(p0) ** self.n
        self._t = t

    def forward(self, lat: float, lon: float) -> tuple[float, float]:
        """(lat, lon) -> (easting, northing)."""
        r = self.A * self.F_ * self._t(math.radians(lat)) ** self.n
        th = self.n * math.radians(lon - self.LON0)
        return self.X0 + r * math.sin(th), self.Y0 + self.r0 - r * math.cos(th)

    def inverse(self, x: float, y: float) -> tuple[float, float]:
        dx, dy = x - self.X0, self.r0 - (y - self.Y0)
        r = math.copysign(math.hypot(dx, dy), self.n)
        t = (r / (self.A * self.F_)) ** (1 / self.n)
        th = math.atan2(dx, dy)
        lat = math.pi / 2 - 2 * math.atan(t)
        for _ in range(8):
            es = self.e * math.sin(lat)
            lat = math.pi / 2 - 2 * math.atan(t * ((1 - es) / (1 + es)) ** (self.e / 2))
        return math.degrees(lat), math.degrees(th / self.n) + self.LON0


LEST = Lest97()


def lest_box_for(mbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """The L-EST97 box that holds a web-mercator box (its edges sampled)."""
    xs, ys = [], []
    for i in range(9):
        f = i / 8
        for mx, my in ((mbox[0] + f * (mbox[2] - mbox[0]), mbox[1]), (mbox[0] + f * (mbox[2] - mbox[0]), mbox[3]),
                       (mbox[0], mbox[1] + f * (mbox[3] - mbox[1])), (mbox[2], mbox[1] + f * (mbox[3] - mbox[1]))):
            e, n = LEST.forward(*unmerc(mx, my))
            xs.append(e)
            ys.append(n)
    return min(xs), min(ys), max(xs), max(ys)


def reproject_lest(img: Image.Image, lbox: tuple[float, float, float, float],
                   mbox: tuple[float, float, float, float], size: tuple[int, int]) -> Image.Image:
    """An L-EST97 picture covering ``lbox`` warped onto the web-mercator box ``mbox``."""
    img = img.convert("RGBA")
    sw, sh = img.size
    w, h = size
    steps = 8

    def src(px: float, py: float) -> tuple[float, float]:
        mx = mbox[0] + px / w * (mbox[2] - mbox[0])
        my = mbox[3] - py / h * (mbox[3] - mbox[1])
        e, n = LEST.forward(*unmerc(mx, my))
        return (e - lbox[0]) / (lbox[2] - lbox[0]) * sw, (lbox[3] - n) / (lbox[3] - lbox[1]) * sh

    mesh = []
    for j in range(steps):
        for i in range(steps):
            x0, x1 = round(i * w / steps), round((i + 1) * w / steps)
            y0, y1 = round(j * h / steps), round((j + 1) * h / steps)
            quad = (*src(x0, y0), *src(x0, y1), *src(x1, y1), *src(x1, y0))
            mesh.append(((x0, y0, x1, y1), quad))
    return img.transform(size, Image.Transform.MESH, mesh, Image.Resampling.BILINEAR, fillcolor=(0, 0, 0, 0))


# --- masks ---------------------------------------------------------------------------
def _all(img: Image.Image, test) -> Image.Image:
    """L mask: 255 where every channel passes ``test``."""
    r, g, b = img.convert("RGB").split()
    lut = [255 if test(v) else 0 for v in range(256)]
    return ImageChops.darker(ImageChops.darker(r.point(lut), g.point(lut)), b.point(lut))


def _alpha(img: Image.Image) -> Image.Image:
    return img.getchannel("A") if img.mode in ("RGBA", "LA") else Image.new("L", img.size, 255)


def nodata_candidates(img: Image.Image, opaque: bool) -> Image.Image:
    """Pure black (and pure white on services that can't do see-through) blobs of at
    least BLOB px: what may be a "no picture here" area."""
    cand = _all(img, lambda v: v <= BLACK)
    if opaque:
        cand = ImageChops.lighter(cand, _all(img, lambda v: v >= WHITE))
    return cand.filter(ImageFilter.MinFilter(BLOB)).filter(ImageFilter.MaxFilter(BLOB))


def footprint(img: Image.Image, opaque: bool) -> Image.Image:
    """L mask of the real picture in an overview of a layer's whole area: 0 where it's
    see-through, or pure black/white joined to the edge or to see-through."""
    img = img.convert("RGBA")
    w, h = img.size
    alpha = _alpha(img)
    out_mask = ImageChops.lighter(nodata_candidates(img, opaque), ImageChops.invert(alpha.point(lambda v: 255 if v > 8 else 0)))
    data = bytearray(out_mask.tobytes())   # 255 = could be outside
    seen = bytearray(w * h)
    stack: list[int] = []
    # Seeds: every border pixel that could be outside, and every see-through pixel.
    alpha_b = alpha.tobytes()
    for i in range(w * h):
        if alpha_b[i] <= 8:
            seen[i] = 1
            stack.append(i)
    for x in range(w):
        for i in (x, (h - 1) * w + x):
            if data[i] and not seen[i]:
                seen[i] = 1
                stack.append(i)
    for y in range(h):
        for i in (y * w, y * w + w - 1):
            if data[i] and not seen[i]:
                seen[i] = 1
                stack.append(i)
    while stack:
        i = stack.pop()
        x = i % w
        for j in (i - w, i + w, i - 1 if x else -1, i + 1 if x < w - 1 else -1):
            if 0 <= j < w * h and data[j] and not seen[j]:
                seen[j] = 1
                stack.append(j)
    table = bytes([255, 0] + [0] * 254)
    return Image.frombytes("L", (w, h), bytes(seen).translate(table))


@dataclass
class Cover:
    """Which cells of a layer's area (a web-mercator box) hold picture."""

    mbox: tuple[float, float, float, float]
    w: int
    h: int
    bits: bytearray  # w*h, row 0 at the top (north)

    @classmethod
    def from_mask(cls, mask: Image.Image, mbox, long: int = GRID_LONG) -> "Cover":
        gw, gh = overview_size(mbox, long)
        small = mask.resize((gw, gh), Image.Resampling.BOX)
        return cls(tuple(mbox), gw, gh, bytearray(1 if v > 6 else 0 for v in small.tobytes()))

    def rle(self) -> str:
        """Run lengths over the cells row by row, starting with an empty run."""
        runs, cur, n = [], 0, 0
        for b in self.bits:
            if b == cur:
                n += 1
            else:
                runs.append(n)
                cur, n = b, 1
        runs.append(n)
        return ",".join(map(str, runs))

    @classmethod
    def from_rle(cls, mbox, w: int, h: int, text: str) -> "Cover":
        bits = bytearray()
        cur = 0
        for part in text.split(","):
            bits.extend([cur] * int(part))
            cur ^= 1
        bits = bits[: w * h] + bytearray(max(0, w * h - len(bits)))
        return cls(tuple(mbox), w, h, bits)

    def share(self) -> float:
        return sum(self.bits) / max(1, len(self.bits))

    def cells(self, mb: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
        """Cell range (i0, j0, i1, j1), inclusive, that a mercator box falls on (may run off the grid)."""
        x0, y0, x1, y1 = self.mbox
        cw, ch = (x1 - x0) / self.w, (y1 - y0) / self.h
        return (math.floor((mb[0] - x0) / cw), math.floor((y1 - mb[3]) / ch),
                math.floor((mb[2] - x0) / cw - 1e-9), math.floor((y1 - mb[1]) / ch - 1e-9))

    def at(self, i: int, j: int) -> int:
        return self.bits[j * self.w + i] if 0 <= i < self.w and 0 <= j < self.h else 0

    def any_in(self, mb, margin: int = 0) -> bool:
        i0, j0, i1, j1 = self.cells(mb)
        for j in range(max(0, j0 - margin), min(self.h - 1, j1 + margin) + 1):
            row = j * self.w
            for i in range(max(0, i0 - margin), min(self.w - 1, i1 + margin) + 1):
                if self.bits[row + i]:
                    return True
        return False

    def edge_zone(self, mb, size: int = 256) -> Image.Image | None:
        """L mask over a tile: 255 on cells that are empty or touch an empty cell, where
        black/white may be "no picture". None if the tile is wholly inside the picture."""
        i0, j0, i1, j1 = self.cells(mb)
        x0, y0, x1, y1 = self.mbox
        cw, ch = (x1 - x0) / self.w, (y1 - y0) / self.h
        zone = None
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                if self.at(i, j) and all(self.at(i + di, j + dj) for di in (-1, 0, 1) for dj in (-1, 0, 1)):
                    continue  # deep inside the picture
                if zone is None:
                    zone = Image.new("L", (size, size), 0)
                # The cell in tile pixels.
                px0 = (x0 + i * cw - mb[0]) / (mb[2] - mb[0]) * size
                px1 = (x0 + (i + 1) * cw - mb[0]) / (mb[2] - mb[0]) * size
                py0 = (mb[3] - (y1 - j * ch)) / (mb[3] - mb[1]) * size
                py1 = (mb[3] - (y1 - (j + 1) * ch)) / (mb[3] - mb[1]) * size
                zone.paste(255, (max(0, math.floor(px0)), max(0, math.floor(py0)),
                                 min(size, math.ceil(px1)), min(size, math.ceil(py1))))
        return zone


def clean_tile(img: Image.Image, zone: Image.Image | None, opaque: bool) -> Image.Image | None:
    """The tile with "no picture" areas see-through: pure black (or white) blobs inside
    ``zone`` (every pixel if None), plus the dark/light seam round them. None if nothing
    is left."""
    img = img.convert("RGBA")
    cand = nodata_candidates(img, opaque)
    if zone is not None:
        cand = ImageChops.darker(cand, zone.resize(img.size) if zone.size != img.size else zone)
    if cand.getbbox():
        halo = _all(img, lambda v: v <= HALO_DARK)
        if opaque:
            halo = ImageChops.lighter(halo, _all(img, lambda v: v >= HALO_LIGHT))
        grown = cand.filter(ImageFilter.MaxFilter(5))
        cand = ImageChops.lighter(cand, ImageChops.darker(grown, halo))
        alpha = ImageChops.darker(_alpha(img), ImageChops.invert(cand))
        # A soft half-pixel edge instead of a jagged one.
        alpha = ImageChops.darker(alpha, alpha.filter(ImageFilter.BoxBlur(1)).point(lambda v: min(255, v * 2)))
        img.putalpha(alpha)
    a = _alpha(img)
    if not a.point(lambda v: 255 if v > 8 else 0).getbbox():
        return None
    return img


def encode(img: Image.Image, photo: bool) -> bytes:
    """WebP (small, keeps see-through); JPEG for a photo tile with nothing see-through."""
    out = io.BytesIO()
    lo, _ = _alpha(img).getextrema()
    if photo and lo == 255:
        img.convert("RGB").save(out, "JPEG", quality=84, optimize=True)
    else:
        try:
            img.save(out, "WEBP", quality=84 if photo else 90, method=4)
        except (OSError, KeyError):  # Pillow without WebP
            out = io.BytesIO()
            img.save(out, "PNG", optimize=False)
    return out.getvalue()
