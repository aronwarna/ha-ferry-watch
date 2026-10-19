"""A seamless nautical chart drawn from Finland's open chart data.

Traficom's chart service is scanned paper chart sheets, so sheet edges and inset
boxes show where they meet. This draws the chart itself instead, the way apps
like Luotsi do, from:

- Traficom's depth data (depth areas, contours, soundings),
- Väylävirasto's fairway data (fairway lines and areas, leading lines, buoys,
  beacons, lights and restricted areas),
- the National Land Survey's rocks, wrecks and place names (with an API key), and
- Museovirasto's register of wrecks, underwater heritage sites and coastal defences.

Data is fetched lazily in blocks (zoom-11 map tiles, about 10 km across) the
first time part of the map is viewed, kept on the NAS for a month, and cut into
small map tiles on request. Land is whatever isn't water: the panel paints the
Finnish area in the land colour and draws the depth areas over it, so islands
line up exactly with the depths around them.

RAM is the scarce resource on a small NAS, so blocks are stored and kept in memory
as flat int32 arrays (about 8 bytes a vertex, where parsed JSON takes about 150),
each block file holds three levels of detail so zoomed-out tiles only read a
simplified copy, and downloads are parsed a page at a time.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import os
import sys
import time
from array import array
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterator

import aiohttp

from .charts import FINNISH_WATERS
from .config import USER_AGENT

_LOGGER = logging.getLogger(__name__)

TRAFICOM_WFS = "https://julkinen.traficom.fi/inspirepalvelu/rajoitettu/wfs"
TRAFICOM_OPEN_WFS = "https://julkinen.traficom.fi/inspirepalvelu/avoin/wfs"
VAYLA_WFS = "https://avoinapi.vaylapilvi.fi/vaylatiedot/vesivaylatiedot/wfs"
# National Land Survey topographic database (rocks, wrecks, names); needs a free API key.
MML_API = "https://avoin-paikkatieto.maanmittauslaitos.fi/maastotiedot/features/v1"
# Finnish Heritage Agency's registers (CC BY 4.0, no key).
MV_WFS = "https://geoserver.museovirasto.fi/geoserver/rajapinta/wfs"
ATTRIBUTION = ("Chart data: Traficom (depths), Väylävirasto (fairways, aids), "
               "Maanmittauslaitos (rocks, names), Museovirasto (wrecks, heritage sites and defences, CC BY 4.0). "
               "Not for navigation.")

BLOCK_Z = 11  # blocks are zoom-11 map tiles, about 10 km across
MIN_Z = 10  # below this the panel shows Traficom's overview chart
UNITS = 65536  # block coordinates: 0..UNITS across a block, y down
EXTENT = 512  # tile coordinates sent to the panel: 0..EXTENT
TILE_BUFFER = 24  # tile units drawn past each edge, so lines and areas meet seamlessly
POINT_BUFFER = 64  # wider for points: symbols and their text reach past the tile edge
POINT_BUFFERS = {"sector": 320, "aid": 128, "light": 128, "name": 256, "sounding": 128}
BLOCK_POINT_MARGIN = UNITS // 4  # points are kept this far past each block edge
MAX_AGE = 30 * 86400
FAILED_RETRY = 3600
PAGE = 2000  # features per request; a page over MAX_RESPONSE is asked for again in halves
MML_PAGE = 1000
MAX_RESPONSE = 8 * 1024 * 1024  # bytes; a bigger page is asked for again in halves
MEMORY_BYTES = 24 * 1024 * 1024  # parsed blocks kept in RAM, by their array sizes...
MEMORY_BLOCKS = 64  # ...and by count; the rest are read from disk when needed
FORGET_AFTER = 20  # s; a queued block nobody has asked for again since is skipped
TILE_REV = 5  # bump when tiles change, so browsers don't keep drawing cached old ones
HALF = 20037508.342789244  # half the web-mercator world, in metres

# Levels of detail in each block file: (highest zoom it serves, simplification in block
# units, smallest feature kept in block units). Zoomed-out tiles read only the first.
LEVELS = ((11, 24, 64), (13, 8, 16), (99, 0, 0))

# What to fetch: layer key -> (service, type name, kind, properties to keep)
SOURCES: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "depth": (TRAFICOM_WFS, "rajoitettu:DepthArea_A", "area", ("drval1", "drval2")),
    "contour": (TRAFICOM_WFS, "rajoitettu:DepthContour_L", "line", ("valdco",)),
    "sounding": (TRAFICOM_WFS, "rajoitettu:Sounding_P", "point", ("depth",)),
    "farea": (VAYLA_WFS, "vesivaylatiedot:vaylaalueet_uusi", "area", ("tyyppikoodi", "mitoitussyvays", "jnro")),
    "fline": (VAYLA_WFS, "vesivaylatiedot:navigointilinjat_uusi", "line", ("mitoitussyvays", "jnro")),
    "lead": (VAYLA_WFS, "vesivaylatiedot:taululinjat_uusi", "line", ("suunta",)),
    "route": (VAYLA_WFS, "vesivaylatiedot:vaylat_uusi", "line", ("vaylaluokkakoodi", "jnro")),
    "restrict": (VAYLA_WFS, "vesivaylatiedot:rajoitusalue_a_uusi", "area", ("rajoitustyypit", "suuruus")),
    "aid": (VAYLA_WFS, "vesivaylatiedot:turvalaitteet_uusi", "point",
            ("turvalaitetyyppifi", "navigointilajikoodi", "valaistu", "nimifi")),
    "sector": (VAYLA_WFS, "vesivaylatiedot:valosektorit_uusi", "point", ("alkukulma", "loppukulma", "vari")),
    "shore": (TRAFICOM_OPEN_WFS, "avoin:ShorelineConstruction_L", "line", ()),
    "light": (VAYLA_WFS, "vesivaylatiedot:loistot_uusi", "point", ("viralvalotunnuse",)),
    "rock": (MML_API, "vesikivi", "point", ("kohdeluokka",)),
    "rockarea": (MML_API, "vesikivikko", "area", ("kohdeluokka",)),
    "wreck": (MML_API, "hylky", "point", ("kohdeluokka",)),
    "name": (MML_API, "paikannimi", "point", ("teksti", "kieli", "kohdeluokka")),
}

# Each block is stored in parts, one file each, so a part added later (or one that
# needs an API key) is fetched on its own without downloading the rest again. A layer
# in a later part replaces the same layer in an earlier one (older base files still
# carry fairways without their class, used until the "fw" part arrives).
PARTS: dict[str, tuple[str, ...]] = {
    "": ("depth", "contour", "sounding", "aid", "sector"),
    "fw": ("farea", "fline", "lead", "route", "restrict"),
    "more": ("shore", "light"),
    "mml": ("rock", "rockarea", "wreck", "name"),
}

# Navigation aid types, as one letter for the panel.
AID_TYPES = {
    "viitta": "v", "poiju": "p", "reunamerkki": "r", "linjamerkki": "l", "kummeli": "k",
    "sektoriloisto": "s", "merimajakka": "m", "tunnusmajakka": "m", "suuntaloisto": "d",
    "apuloisto": "a", "tutkamerkki": "t", "tutkamajakka": "t",
}
MAJOR_AIDS = ("m", "s")  # all that is drawn zoomed out, as on overview charts
# Light sector colours as chart letters, in the order charts print them.
LIGHT_COLOURS = {"v": "W", "p": "R", "vi": "G", "k": "Y"}

# MML feature classes. Rocks: 1 underwater, 2 awash, 3 above water.
ROCK_TYPES = {38511: 1, 38512: 2, 38513: 3}
# Wrecks: 1 depth unknown (dangerous), 2 depth known, 3 hull showing.
WRECK_TYPES = {16721: 1, 16722: 2, 16712: 3}
# Depth contours as the paper chart shows them; Traficom's data has more.
CONTOURS = (3, 6, 10, 20, 50, 100, 200)

# Museovirasto's point layers with underwater sites and wrecks, and the register class
# each one is: 0 protected ancient monument, 1 other heritage site, 2 other site,
# 3 possible ancient monument, 4 find spot, 5 observation.
MV_TYPES = {
    "rajapinta:muinaisjaannos_piste": 0,
    "rajapinta:rajapinta_muu_kulttuuriperintokohde_piste": 1,
    "rajapinta:rajapinta_muu_kohde_piste": 2,
    "rajapinta:rajapinta_mahdollinen_muinaisjaannos_piste": 3,
    "rajapinta:rajapinta_loytopaikka_piste": 4,
    "rajapinta:rajapinta_havaintokohde_piste": 5,
}
MV_FIELDS = ("mjtunnus", "kohdenimi", "tyyppi", "alatyyppi", "ajoitus", "paikannustarkkuus", "vedenalainen")
# Register sub-types in English, matched by the start of each word; the first match wins.
MV_SUBTYPES = (
    ("hylyt (puu)", "wooden wreck"), ("hylyt (metalli)", "metal wreck"), ("hyly", "wreck"),
    ("löytöpaik", "find spot"), ("taisteluhau", "trenches"), ("juoksuhau", "trenches"),
    ("tykkiasem", "gun positions"), ("tykkipatter", "gun batteries"), ("patteri", "gun batteries"),
    ("ampumapes", "firing positions"), ("ampuma-asem", "firing positions"), ("konekivä", "machine-gun nests"),
    ("bunkk", "bunkers"), ("betonilinn", "concrete bunkers"), ("korsu", "dugouts"),
    ("linnoit", "fortifications"), ("varustu", "fortifications"), ("linna", "castle"), ("valli", "ramparts"),
    ("kivirakente", "stone structures"), ("kivilaite", "stone structures"), ("kasarm", "barracks"),
    ("luola", "caves and tunnels"), ("tunnel", "tunnels"), ("panssariest", "tank obstacles"),
    ("este", "obstacles"), ("tähysty", "observation posts"), ("kuop", "pits"), ("kaivann", "pits"),
    ("varikko", "depot"), ("ruutikellar", "powder magazine"), ("kellar", "cellars"),
)
HERITAGE_Z = 7  # heritage sites are few, so they're fetched for whole zoom-7 tiles at once
HERITAGE_REGIONS = 6  # regions kept in RAM

# Zoom from which each layer is drawn.
DEFENCE_MIN_Z = 13  # coastal defence sites on land, only drawn up close
COAST_REACH = UNITS // 12  # block units (about 800 m): land sites further from the sea are left out
LAYER_MIN_Z = {"depth": MIN_Z, "contour": MIN_Z, "sounding": 13, "farea": 11, "fline": MIN_Z,
               "lead": 11, "route": 11, "restrict": 12, "aid": 11, "sector": 11, "shore": 12, "light": 13,
               "rock": 12, "rockarea": 12, "wreck": 12, "name": 11, "heritage": 11}


# --- geometry ------------------------------------------------------------------------

def clip_ring(pts: list[tuple[float, float]], x0: float, y0: float, x1: float, y1: float) -> list:
    """Sutherland-Hodgman: the part of a polygon ring inside the box. Edges along the
    box are fine for filling, which is all rings are used for."""
    for edge in range(4):
        if not pts:
            return pts
        out = []
        px, py = pts[-1]
        for cx, cy in pts:
            if edge == 0:
                cin, pin = cx >= x0, px >= x0
            elif edge == 1:
                cin, pin = cx <= x1, px <= x1
            elif edge == 2:
                cin, pin = cy >= y0, py >= y0
            else:
                cin, pin = cy <= y1, py <= y1
            if cin != pin:
                if edge < 2:
                    xe = x0 if edge == 0 else x1
                    out.append((xe, py + (cy - py) * (xe - px) / (cx - px)))
                else:
                    ye = y0 if edge == 2 else y1
                    out.append((px + (cx - px) * (ye - py) / (cy - py), ye))
            if cin:
                out.append((cx, cy))
            px, py = cx, cy
        pts = out
    return pts


def clip_line(pts: list[tuple[float, float]], x0: float, y0: float, x1: float, y1: float) -> list[list]:
    """Liang-Barsky per segment: the pieces of a line inside the box."""
    parts: list[list] = []
    cur: list = []
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        t0, t1 = 0.0, 1.0
        ok = True
        for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                t = q / p
                if p < 0:
                    if t > t1:
                        ok = False
                        break
                    t0 = max(t0, t)
                else:
                    if t < t0:
                        ok = False
                        break
                    t1 = min(t1, t)
        if not ok:
            if len(cur) > 1:
                parts.append(cur)
            cur = []
            continue
        start = (ax + t0 * dx, ay + t0 * dy)
        end = (ax + t1 * dx, ay + t1 * dy)
        if not cur or cur[-1] != start:
            if len(cur) > 1:
                parts.append(cur)
            cur = [start]
        cur.append(end)
        if t1 < 1.0:
            parts.append(cur)
            cur = []
    if len(cur) > 1:
        parts.append(cur)
    return parts


def simplify(pts: list, tol: float) -> list:
    """Douglas-Peucker with a squared tolerance, keeping the end points."""
    n = len(pts)
    if n < 3:
        return pts
    keep = [False] * n
    keep[0] = keep[-1] = True
    tol2 = tol * tol
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        len2 = dx * dx + dy * dy
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if len2 == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, idx = d, i
        if best > tol2:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(pts, keep) if k]


def _bbox(pts) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _flat(pts) -> list[int]:
    """[(x, y), ...] -> [x0, y0, x1, y1, ...] as whole tile units, dropping repeats."""
    out: list[int] = []
    for x, y in pts:
        xi, yi = round(x), round(y)
        if len(out) >= 2 and out[-2] == xi and out[-1] == yi:
            continue
        out += (xi, yi)
    return out


# --- coordinates ---------------------------------------------------------------------


# --- compact block layers -------------------------------------------------------------

class Layer:
    """One layer of a block as flat int32 arrays instead of nested lists.

    Every row has a property tuple (``props[pi[i]]``; repeated tuples are shared).
    Points: ``co[2i], co[2i+1]``. Lines: vertices ``vo[i]..vo[i+1]``. Areas: rings
    ``rs[i]..rs[i+1]``, ring r being vertices ``vo[r]..vo[r+1]``. Vertex v is
    ``co[2v], co[2v+1]``; lines and areas also have a bounding box, ``bb[4i:4i+4]``.
    """

    __slots__ = ("kind", "props", "pi", "bb", "rs", "vo", "co")
    ARRAYS = {"point": ("pi", "co"), "line": ("pi", "bb", "vo", "co"), "area": ("pi", "bb", "rs", "vo", "co")}

    def __init__(self, kind: str):
        self.kind = kind
        self.props: list = []
        self.pi = array("i")
        self.bb = array("i")
        self.rs = array("i", [0])
        self.vo = array("i", [0])
        self.co = array("i")

    @property
    def n(self) -> int:
        return len(self.pi)

    def nbytes(self) -> int:
        return sum(len(getattr(self, a)) * 4 for a in self.ARRAYS[self.kind]) + 120 * len(self.props) + 400

    def bbox(self, i: int) -> tuple[int, int, int, int]:
        b = 4 * i
        return self.bb[b], self.bb[b + 1], self.bb[b + 2], self.bb[b + 3]

    def rings(self, i: int) -> range:
        return range(self.rs[i], self.rs[i + 1])

    def pts(self, v0: int, v1: int) -> list[tuple[int, int]]:
        co = self.co
        return [(co[2 * v], co[2 * v + 1]) for v in range(v0, v1)]


class LayerBuilder:
    """Appends rows to a Layer, sharing repeated property tuples."""

    def __init__(self, kind: str):
        self.layer = Layer(kind)
        self._index: dict[tuple, int] = {}

    def _row(self, props) -> None:
        key = tuple(props)
        i = self._index.get(key)
        if i is None:
            i = self._index[key] = len(self.layer.props)
            self.layer.props.append(key)
        self.layer.pi.append(i)

    def point(self, props, x: float, y: float) -> None:
        self._row(props)
        self.layer.co.extend((round(x), round(y)))

    def _vertices(self, pts) -> tuple[int, int, int, int] | None:
        """Adds a run of vertices (dropping repeats); its bbox, or None if under two."""
        co, vo = self.layer.co, self.layer.vo
        start = len(co)
        lx = ly = None
        for x, y in pts:
            x, y = round(x), round(y)
            if x == lx and y == ly:
                continue
            co.append(x)
            co.append(y)
            lx, ly = x, y
        if len(co) - start < 4:
            del co[start:]
            return None
        xs, ys = co[start::2], co[start + 1::2]
        vo.append(len(co) // 2)
        return min(xs), min(ys), max(xs), max(ys)

    def line(self, props, pts) -> None:
        bb = self._vertices(pts)
        if bb is not None:
            self._row(props)
            self.layer.bb.extend(bb)

    def area(self, props, rings) -> None:
        L = self.layer
        boxes = []
        for ring in rings:
            mark = len(L.vo)
            bb = self._vertices(ring)
            if bb is not None and L.vo[-1] - L.vo[-2] >= 3:
                boxes.append(bb)
            elif bb is not None:  # fewer than three corners: no area
                del L.co[2 * L.vo[-2]:]
                del L.vo[mark:]
        if not boxes:
            return
        self._row(props)
        L.rs.append(len(L.vo) - 1)
        L.bb.extend((min(b[0] for b in boxes), min(b[1] for b in boxes),
                     max(b[2] for b in boxes), max(b[3] for b in boxes)))


def _scalar(value: Any) -> Any:
    """A property value that can be stored and hashed: lists become comma lists."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def layers_from_rows(rows: dict[str, list]) -> dict[str, Layer]:
    """Layers from rows in the old list form: areas [props, bbox, rings], lines
    [props, bbox, line], points [props, x, y]. Used by tests and to convert old files."""
    out = {}
    for key, items in rows.items():
        kind = SOURCES[key][2] if key in SOURCES else "point"
        b = LayerBuilder(kind)
        for row in items:
            _add_row(b, row)
        out[key] = b.layer
    return out


def _add_row(b: LayerBuilder, row: list) -> None:
    if b.layer.kind == "point":
        b.point([_scalar(v) for v in row[0]], row[1], row[2])
    elif b.layer.kind == "line":
        b.line([_scalar(v) for v in row[0]], row[2])
    else:
        b.area([_scalar(v) for v in row[0]], row[2])


def _dp(co: array, v0: int, v1: int, tol: float) -> list[int]:
    """Douglas-Peucker on vertices v0..v1-1 of a flat coordinate array: the ones to keep."""
    n = v1 - v0
    if n < 3 or tol <= 0:
        return list(range(v0, v1))
    keep = bytearray(n)
    keep[0] = keep[-1] = 1
    tol2 = tol * tol
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = co[2 * (v0 + a)], co[2 * (v0 + a) + 1]
        bx, by = co[2 * (v0 + b)], co[2 * (v0 + b) + 1]
        dx, dy = bx - ax, by - ay
        len2 = dx * dx + dy * dy
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = co[2 * (v0 + i)], co[2 * (v0 + i) + 1]
            if len2 == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, idx = d, i
        if best > tol2:
            keep[idx] = 1
            stack.append((a, idx))
            stack.append((idx, b))
    return [v0 + i for i in range(n) if keep[i]]


def _point_shown(key: str, props: tuple, max_z: int) -> bool:
    """Whether a point could be drawn at or below a zoom (zoomed-out levels skip the rest)."""
    if key == "aid":
        return max_z >= 12 or AID_TYPES.get(str(props[0] or "").lower()) in MAJOR_AIDS
    if key == "name":
        text = str(props[0] or "")
        code = _int(props[2]) if len(props) > 2 else None
        return _name_min_z(code, _name_kind(code, text), text) <= max_z
    return True


def _reduce(key: str, layer: Layer, max_z: int, tol: float, smallest: int) -> Layer:
    """A simplified copy of a layer for zoomed-out tiles."""
    b = LayerBuilder(layer.kind)
    co = layer.co
    for i in range(layer.n):
        props = layer.props[layer.pi[i]]
        if layer.kind == "point":
            if _point_shown(key, props, max_z):
                b.point(props, co[2 * i], co[2 * i + 1])
            continue
        x0, y0, x1, y1 = layer.bbox(i)
        if x1 - x0 < smallest and y1 - y0 < smallest:
            continue
        if key == "contour" and (_num(props[0]) not in CONTOURS or (max_z < 12 and _num(props[0]) < 10)):
            continue
        if layer.kind == "line":
            b.line(props, [(co[2 * v], co[2 * v + 1]) for v in _dp(co, layer.vo[i], layer.vo[i + 1], tol)])
        else:
            rings = []
            for r in layer.rings(i):
                kept = _dp(co, layer.vo[r], layer.vo[r + 1], tol)
                if len(kept) >= 3:
                    rings.append([(co[2 * v], co[2 * v + 1]) for v in kept])
            b.area(props, rings)
    return b.layer


def make_levels(full: dict[str, Layer]) -> list[dict[str, Layer]]:
    """The levels of detail stored in a block file, from the full data."""
    levels = []
    for max_z, tol, smallest in LEVELS:
        if not tol:
            levels.append(full)
            continue
        levels.append({key: _reduce(key, layer, max_z, tol, smallest) for key, layer in full.items()
                       if LAYER_MIN_Z.get(key, 99) <= max_z})
    return levels


# --- block files ----------------------------------------------------------------------
# A block part file is gzip: a magic line, then for each level of detail a JSON header
# line (layer kinds, property tuples, array lengths) followed by the raw int32 arrays.
# Reading a zoomed-out level stops early, so it never touches the detailed data.

MAGIC = b"FWCHART2\n"
SUFFIX = ".fwc"
OLD_SUFFIX = ".json.gz"  # the earlier JSON format, converted on first use


def write_block(path: Path, meta: dict, levels: list[dict[str, Layer]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wb", compresslevel=5) as f:
        f.write(MAGIC)
        for layers in levels:
            head = {**meta, "layers": {key: {"k": L.kind, "p": L.props,
                                              "n": [len(getattr(L, a)) for a in Layer.ARRAYS[L.kind]]}
                                        for key, L in layers.items()}}
            f.write(json.dumps(head, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
            for L in layers.values():
                for a in Layer.ARRAYS[L.kind]:
                    arr = getattr(L, a)
                    if sys.byteorder != "little":
                        arr = array("i", arr)
                        arr.byteswap()
                    arr.tofile(f)
    os.replace(tmp, path)


def read_block(path: Path, level: int) -> tuple[dict, dict[str, Layer]]:
    """(meta, layers) of one level of a block file."""
    with gzip.open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path.name} is not a chart block")
        for lvl in range(level + 1):
            head = json.loads(f.readline())
            layers: dict[str, Layer] = {}
            for key, spec in head.pop("layers").items():
                names = Layer.ARRAYS[spec["k"]]
                if lvl < level:
                    f.seek(4 * sum(spec["n"]), 1)
                    continue
                L = Layer(spec["k"])
                L.props = [tuple(p) for p in spec["p"]]
                for name, n in zip(names, spec["n"]):
                    arr = array("i")
                    arr.fromfile(f, n)
                    if sys.byteorder != "little":
                        arr.byteswap()
                    setattr(L, name, arr)
                layers[key] = L
        return head, layers


def read_meta(path: Path) -> dict:
    """Just the header of a block file (whether layers were missing, and so on)."""
    with gzip.open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path.name} is not a chart block")
        head = json.loads(f.readline())
    head.pop("layers", None)
    return head


def _iter_old(path: Path) -> Iterator[tuple[str, Any]]:
    """Rows of an old JSON block file one at a time, so converting a big file never
    holds it all in memory: ("missing", [...]) first, then (layer, row) pairs."""
    dec = json.JSONDecoder()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        buf, pos = "", 0

        def fill() -> bool:
            nonlocal buf, pos
            chunk = f.read(1 << 16)
            if not chunk:
                return False
            buf, pos = buf[pos:] + chunk, 0
            return True

        def char() -> str:
            while pos >= len(buf):
                if not fill():
                    raise ValueError("chart block ends early")
            return buf[pos]

        def value() -> Any:
            nonlocal pos
            while True:
                try:
                    v, end = dec.raw_decode(buf, pos)
                except json.JSONDecodeError:
                    if not fill():
                        raise
                    continue
                pos = end
                return v

        while '"layers":{' not in buf:
            if not fill():
                raise ValueError("no layers in chart block")
        start = buf.index('"missing":') + len('"missing":') if '"missing":' in buf else -1
        if start >= 0:
            pos = start
            yield "missing", value()
        pos = buf.index('"layers":{') + len('"layers":{')
        while char() != "}":
            if char() == ",":
                pos += 1
            key = value()
            for expected in ":[":
                if char() != expected:
                    raise ValueError("unexpected chart block layout")
                pos += 1
            while char() != "]":
                if char() == ",":
                    pos += 1
                yield key, value()
            pos += 1


def convert_old(path: Path) -> tuple[dict, list[dict[str, Layer]]]:
    """An old JSON block file as (meta, levels)."""
    builders: dict[str, LayerBuilder] = {}
    missing: list = []
    for key, row in _iter_old(path):
        if key == "missing":
            missing = row
            continue
        if key not in SOURCES:
            continue
        b = builders.get(key)
        if b is None:
            b = builders[key] = LayerBuilder(SOURCES[key][2])
        _add_row(b, row)
    full = {key: b.layer for key, b in builders.items()}
    return {"v": 2, "missing": missing}, make_levels(full)


def block_bounds(bx: int, by: int) -> tuple[float, float, float, float]:
    """Web-mercator metres of a block: (minx, miny, maxx, maxy)."""
    size = 2 * HALF / 2**BLOCK_Z
    minx = -HALF + bx * size
    maxy = HALF - by * size
    return minx, maxy - size, minx + size, maxy


def to_block(bx: int, by: int) -> Callable[[float, float], tuple[float, float]]:
    minx, _, _, maxy = block_bounds(bx, by)
    scale = UNITS / (2 * HALF / 2**BLOCK_Z)
    return lambda x, y: ((x - minx) * scale, (maxy - y) * scale)


def lonlat_to_mercator(lon: float, lat: float) -> tuple[float, float]:
    x = lon * HALF / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) * HALF / math.pi
    return x, y


FINNISH_WATERS_M = [lonlat_to_mercator(lon, lat) for lon, lat in FINNISH_WATERS]
NEAR_M = 30000  # Finnish chart data reaches a little past FINNISH_WATERS


def near_finland(bounds: tuple[float, float, float, float]) -> bool:
    """Whether a block could hold Finnish chart data (it touches FINNISH_WATERS, give or take)."""
    minx, miny, maxx, maxy = bounds[0] - NEAR_M, bounds[1] - NEAR_M, bounds[2] + NEAR_M, bounds[3] + NEAR_M
    poly = FINNISH_WATERS_M
    if any(minx <= x <= maxx and miny <= y <= maxy for x, y in poly):
        return True
    for i in range(5):
        for j in range(5):
            px, py = minx + (maxx - minx) * i / 4, miny + (maxy - miny) * j / 4
            inside = False
            k = len(poly) - 1
            for n, (xi, yi) in enumerate(poly):
                xj, yj = poly[k]
                if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                    inside = not inside
                k = n
            if inside:
                return True
    return False


# Kept on the NAS when "Keep chart on NAS" is on: the Gulf of Finland, Hanko to the
# eastern border (lon/lat). Blocks in it with no Finnish waters nearby are left out.
KEEP_AREA = (22.7, 59.45, 28.2, 60.75)
KEEP_PAUSE = 1.0  # s between downloads, to be gentle on Traficom and Väylävirasto
KEEP_RECHECK = 6 * 3600  # s between passes that refresh blocks older than MAX_AGE


def keep_blocks(area: tuple[float, float, float, float] = KEEP_AREA) -> list[tuple[int, int]]:
    """Blocks to keep on disk for an area, nearest the middle of the gulf first."""
    n = 2**BLOCK_Z
    (x0, y1), (x1, y0) = lonlat_to_mercator(area[0], area[1]), lonlat_to_mercator(area[2], area[3])
    size = 2 * HALF / n
    bx0, bx1 = int((x0 + HALF) // size), int((x1 + HALF) // size)
    by0, by1 = int((HALF - y0) // size), int((HALF - y1) // size)
    return [(bx, by) for by in range(by0, by1 + 1) for bx in range(bx0, bx1 + 1)
            if near_finland(block_bounds(bx, by))]


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- turning WFS features into block data -------------------------------------------

def _rings(geom: dict) -> list[list]:
    """Polygon rings (outer and holes alike) of a Polygon or MultiPolygon."""
    if geom["type"] == "Polygon":
        return list(geom["coordinates"])
    if geom["type"] == "MultiPolygon":
        return [ring for poly in geom["coordinates"] for ring in poly]
    return []


def _lines(geom: dict) -> list[list]:
    if geom["type"] == "LineString":
        return [geom["coordinates"]]
    if geom["type"] == "MultiLineString":
        return list(geom["coordinates"])
    return []


def _points(geom: dict) -> list:
    if geom["type"] == "Point":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPoint":
        return list(geom["coordinates"])
    return []



def build_into(b: LayerBuilder, bx: int, by: int, key: str, features: list[dict]) -> None:
    """Clip a page of a layer's features to the block (with a margin) and add what the
    panel draws to the layer being built."""
    project = to_block(bx, by)
    kind, wanted = SOURCES[key][2], SOURCES[key][3]
    margin = UNITS * TILE_BUFFER / EXTENT  # a block-sized tile's buffer, the largest needed
    lo, hi = -margin, UNITS + margin
    plo, phi = -BLOCK_POINT_MARGIN, UNITS + BLOCK_POINT_MARGIN
    for f in features:
        geom = f.get("geometry")
        if not geom:
            continue
        raw = {k.lower(): v for k, v in (f.get("properties") or {}).items()}
        props = [_scalar(raw.get(p)) for p in wanted]
        if kind == "area":
            rings = []
            for ring in _rings(geom):
                clipped = clip_ring([project(*c[:2]) for c in ring], lo, lo, hi, hi)
                if len(clipped) >= 3:
                    rings.append(clipped)
            b.area(props, rings)
        elif kind == "line":
            for line in _lines(geom):
                for part in clip_line([project(*c[:2]) for c in line], lo, lo, hi, hi):
                    b.line(props, part)
        else:
            for c in _points(geom):
                x, y = project(*c[:2])
                if plo <= x <= phi and plo <= y <= phi:
                    b.point(props, x, y)


def build_block(bx: int, by: int, layers: dict[str, list[dict]]) -> dict[str, Layer]:
    """All of a block's layers from WFS features at once (tests and the simulator)."""
    out = {}
    for key, features in layers.items():
        b = LayerBuilder(SOURCES[key][2])
        build_into(b, bx, by, key, features)
        out[key] = b.layer
    return out


def heritage_props(feature: dict) -> tuple | None:
    """A Museovirasto site as (register id, name, class, kind, dating, position accuracy,
    sub-type in English). Kind: 1 wooden wreck, 2 metal wreck, 3 other or unknown wreck,
    4 other underwater site, 5 defence site on land (bunkers, trenches, gun positions)."""
    p = {k.lower(): v for k, v in (feature.get("properties") or {}).items()}
    site = _int(p.get("mjtunnus"))
    if site is None:
        return None
    sub = str(p.get("alatyyppi") or "").lower()
    kinds = str(p.get("tyyppi") or "").lower()
    if "hylyt (puu)" in sub:
        kind = 1
    elif "hylyt (metalli)" in sub:
        kind = 2
    elif "hyly" in kinds or "hyly" in sub or "haaksirikko" in sub:
        kind = 3
    elif "puolustus" in kinds and str(p.get("vedenalainen") or "").lower() != "k":
        kind = 5
    else:
        kind = 4

    def text(key: str) -> str | None:
        v = str(p.get(key) or "").strip()
        return v or None

    return (site, text("kohdenimi"), int(p.get("_class", 2)), kind, text("ajoitus"), text("paikannustarkkuus"),
            _subtype(sub))


def _subtype(sub: str) -> str | None:
    """Museovirasto's sub-types ("tykkiasemat, taisteluhaudat") in English."""
    out: list[str] = []
    for part in sub.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name = next((en for fi, en in MV_SUBTYPES if fi in part), part)
        if name not in out:
            out.append(name)
    return ", ".join(out) or None


def coastal_filter(sites: Layer, depth: Layer | None) -> Layer:
    """Drop land sites (kind 5) further than COAST_REACH from the sea, so only the coast and
    islands show defences. Depth area edges mark the shore; edges where a depth area was cut
    at the block margin are not shore."""
    if not any(sites.props[sites.pi[i]][3] == 5 for i in range(sites.n)):
        return sites
    cell = COAST_REACH // 2
    shore: set[tuple[int, int]] = set()
    if depth is not None:
        margin = UNITS * TILE_BUFFER / EXTENT
        lo, hi = round(-margin), round(UNITS + margin)
        co = depth.co
        for i in range(depth.n):
            for r in depth.rings(i):
                v0, v1 = depth.vo[r], depth.vo[r + 1]
                for v in range(v0, v1):
                    w = v + 1 if v + 1 < v1 else v0
                    ax, ay, bx, by = co[2 * v], co[2 * v + 1], co[2 * w], co[2 * w + 1]
                    if (ax == bx and (ax <= lo or ax >= hi)) or (ay == by and (ay <= lo or ay >= hi)):
                        continue
                    steps = max(abs(bx - ax), abs(by - ay)) // cell + 1
                    for k in range(steps + 1):
                        shore.add((int(ax + (bx - ax) * k / steps) // cell, int(ay + (by - ay) * k / steps) // cell))
    b = LayerBuilder("point")
    for i in range(sites.n):
        props = sites.props[sites.pi[i]]
        x, y = sites.co[2 * i], sites.co[2 * i + 1]
        if props[3] == 5:
            cx, cy = x // cell, y // cell
            if not any((cx + dx, cy + dy) in shore for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)):
                continue
        b.point(props, x, y)
    return b.layer



# --- cutting tiles --------------------------------------------------------------------

def _depth_band(drval1: Any, drval2: Any) -> int:
    """0: drying, 1: 0-3 m, 2: 3-6 m, 3: 6-10 m, 4: deeper (white)."""
    d1 = _num(drval1)
    d2 = _num(drval2)
    if d1 is not None and d1 < 0 and (d2 is None or d2 <= 0):
        return 0
    top = d2 if d2 is not None else d1
    if top is None:
        return 4
    if top <= 3:
        return 1
    if top <= 6:
        return 2
    if top <= 10:
        return 3
    return 4


def level_for(z: int) -> int:
    """Which stored level of detail a zoom level's tiles are cut from."""
    return next(i for i, (max_z, _, _) in enumerate(LEVELS) if z <= max_z)


def _run_pts(layer: Layer, v0: int, v1: int, x0: float, y0: float, x1: float, y1: float) -> list:
    """Vertices v0..v1-1, with each run that lies wholly beyond one side of the box cut
    down to its two ends. Clipping to the box gives the same result, and a long coast
    that only touches the tile costs a few comparisons per vertex, not a tuple each."""
    co = layer.co
    out = []
    code = 0  # the outside sides every vertex of the current run shares
    last = None  # the run's latest vertex, not yet added
    for v in range(v0, v1):
        x, y = co[2 * v], co[2 * v + 1]
        c = (x < x0) | (x > x1) << 1 | (y < y0) << 2 | (y > y1) << 3
        if code and c & code:
            code &= c
            last = (x, y)
            continue
        if last is not None:
            out.append(last)
            last = None
        out.append((x, y))
        code = c
    if last is not None:
        out.append(last)
    return out


def _fairway_classes(blocks: dict[tuple[int, int], dict | None]) -> dict[int, int]:
    """Fairway number -> class (1 main merchant fairway ... 6), from the blocks' routes."""
    classes: dict[int, int] = {}
    for data in blocks.values():
        route = (data or {}).get("route")
        for props in route.props if route else ():
            jnro, vl = _int(props[1]), _int(props[0])
            if jnro is not None and vl is not None:
                classes[jnro] = vl
    return classes


def make_tile(z: int, x: int, y: int, blocks: dict[tuple[int, int], dict | None]) -> dict[str, Any]:
    """The chart features for one map tile, in tile units (0..EXTENT, y down)."""
    tile: dict[str, Any] = {"e": EXTENT}
    rows: dict[str, list] = {}  # layer -> [props, geometry...] or [x, y, props], before formatting
    lo, hi = -TILE_BUFFER, EXTENT + TILE_BUFFER
    tol = 0.35 if z >= 12 else 0.5
    land_rects: list[tuple[float, float, float, float]] = []
    seen_points: set = set()
    for (bx, by), data in blocks.items():
        if not data:
            continue
        # Block units -> tile units, and the tile (with buffer) in block units for quick skips.
        if z >= BLOCK_Z:
            n = 2 ** (z - BLOCK_Z)
            size = UNITS / n
            ox, oy = (x - bx * n) * size, (y - by * n) * size
        else:
            n = 2 ** (BLOCK_Z - z)
            size = UNITS * n
            ox, oy = (x * n - bx) * UNITS, (y * n - by) * UNITS
        k = EXTENT / size
        bx0, by0 = ox + lo / k, oy + lo / k
        bx1, by1 = ox + hi / k, oy + hi / k

        def conv(pts):
            return [((px - ox) * k, (py - oy) * k) for px, py in pts]

        depth = data.get("depth")
        if depth is not None and depth.n:
            # Land is painted only where this block's depths are, so a block that
            # couldn't be fetched never shows up as solid land. (1 unit of overlap
            # hides the seam between blocks.)
            land_rects.append((max(lo, -ox * k - 1), max(lo, -oy * k - 1),
                               min(hi, (UNITS - ox) * k + 1), min(hi, (UNITS - oy) * k + 1)))

        for key, layer in data.items():
            if key[0] == "_" or z < LAYER_MIN_Z.get(key, 99):
                continue
            out = rows.setdefault(key, [])
            props, pi = layer.props, layer.pi
            if layer.kind == "point":
                buf = POINT_BUFFERS.get(key, POINT_BUFFER)
                px0, py0 = ox - buf / k, oy - buf / k
                px1, py1 = ox + (EXTENT + buf) / k, oy + (EXTENT + buf) / k
                co = layer.co
                for i in range(layer.n):
                    px, py = co[2 * i], co[2 * i + 1]
                    if not (px0 <= px <= px1 and py0 <= py <= py1):
                        continue
                    tx, ty = round((px - ox) * k), round((py - oy) * k)
                    p = props[pi[i]]
                    seen = (key, tx, ty, p)
                    if seen in seen_points:
                        continue  # the same point from two blocks' margins
                    seen_points.add(seen)
                    out.append([tx, ty, p])
                continue
            for i in range(layer.n):
                fx0, fy0, fx1, fy1 = layer.bbox(i)
                if fx1 < bx0 or fx0 > bx1 or fy1 < by0 or fy0 > by1:
                    continue
                if (fx1 - fx0) * k < 0.6 and (fy1 - fy0) * k < 0.6:
                    continue  # too small to see at this zoom
                p = props[pi[i]]
                if layer.kind == "area":
                    rings = []
                    for r in layer.rings(i):
                        pts = _run_pts(layer, layer.vo[r], layer.vo[r + 1], bx0, by0, bx1, by1)
                        part = simplify(clip_ring(conv(pts), lo, lo, hi, hi), tol)
                        if len(part) >= 3:
                            rings.append(_flat(part))
                    if rings:
                        out.append([p, *rings])
                    continue
                if key == "contour":
                    d = _num(p[0])
                    if d not in CONTOURS or (z < 12 and d < 10):
                        continue
                if key == "fline" and z < 11 and p[0] is None:
                    continue  # boat routes only once zoomed in
                pts = _run_pts(layer, layer.vo[i], layer.vo[i + 1], bx0, by0, bx1, by1)
                for part in clip_line(conv(pts), lo, lo, hi, hi):
                    flat = _flat(simplify(part, tol))
                    if len(flat) >= 4:
                        out.append([p, flat])
    if land_rects:
        # Land: the Finnish area, painted under the depth areas.
        minx, miny, maxx, maxy = _tile_mercator(z, x, y)
        k = EXTENT / (maxx - minx)
        ring = [((mx - minx) * k, (maxy - my) * k) for mx, my in FINNISH_WATERS_M]
        land = [_flat(part) for part in (clip_ring(ring, *rect) for rect in land_rects) if len(part) >= 3]
        if land:
            tile["land"] = land
    _format(tile, rows, z, _fairway_classes(blocks))
    return {k: v for k, v in tile.items() if v != []}


def _pad(p: tuple, n: int) -> tuple:
    """Properties padded to n values (older files kept fewer for some layers)."""
    return p if len(p) >= n else p + (None,) * (n - len(p))


def _format(tile: dict, rows: dict[str, list], z: int, classes: dict[int, int]) -> None:
    """Turn collected rows into what the panel reads (see static/vchart.js)."""
    def vl(jnro: Any) -> int | None:
        return classes.get(_int(jnro)) if jnro is not None else None

    if "depth" in rows:
        tile["depth"] = sorted(([_depth_band(*_pad(p, 2)[:2]), *rings] for p, *rings in rows["depth"]),
                               key=lambda row: -row[0])  # deep first
    if "contour" in rows:
        tile["contour"] = [[_num(p[0]), line] for p, line in rows["contour"]]
    if "farea" in rows:
        tile["farea"] = [[_int(code), _num(draught), vl(jnro), *rings]
                         for p, *rings in rows["farea"] for code, draught, jnro in [_pad(p, 3)[:3]]]
    if "fline" in rows:
        tile["fline"] = [[_num(draught), vl(jnro), line]
                         for p, line in rows["fline"] for draught, jnro in [_pad(p, 2)[:2]]]
    if "lead" in rows:
        tile["lead"] = [[_num(_pad(p, 1)[0]), line] for p, line in rows["lead"]]
    if "route" in rows:
        # The small-craft trunk route (class 4) gets the chart's green band.
        tile["band"] = [[line] for p, line in rows["route"] if _int(p[0]) == 4]
    if "restrict" in rows:
        tile["restrict"] = [[p[0], _num(p[1]), *rings] for p, *rings in rows["restrict"]]
    if "shore" in rows:
        tile["shore"] = [[line] for _, line in rows["shore"]]
    if "rockarea" in rows:
        tile["rockarea"] = [[ROCK_TYPES.get(_int(_pad(p, 1)[0]), 0), *rings] for p, *rings in rows["rockarea"]]

    colours: dict[tuple[int, int], set] = {}
    for px, py, (a, b, c) in rows.get("sector", ()):
        letter = LIGHT_COLOURS.get(str(c or "").lower())
        if letter:
            colours.setdefault((px, py), set()).add(letter)
    if "sector" in rows:
        # Light sectors: [x, y, from°, to°, colour], bearings towards the light as charts give them.
        # All-round rows only give a plain light its colour, read above.
        tile["sector"] = [[px, py, a, b, str(c or "v")] for px, py, a, b, c in
                          ((px, py, _num(a), _num(b), c) for px, py, (a, b, c) in rows["sector"])
                          if a is not None and b is not None and (b - a) % 360 != 0]
    if "aid" in rows:
        aids = []
        for px, py, (kind, cat, lit, name) in rows["aid"]:
            letter = AID_TYPES.get(str(kind or "").lower(), "o")
            lit = 1 if lit == "K" else 0
            # Zoomed out, only the major lights; then lit marks; all from zoom 13, as on charts.
            if (z <= 11 and letter not in MAJOR_AIDS) or (z == 12 and not lit and letter not in MAJOR_AIDS):
                continue
            found = colours.get((px, py)) or next(
                (colours[(px + dx, py + dy)] for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (px + dx, py + dy) in colours),
                set())
            aids.append([px, py, letter, _int(cat), lit, name, "".join(ch for ch in "WRGY" if ch in found)])
        tile["aid"] = aids
    if "light" in rows:
        # Light characters as printed next to the light, e.g. "Iso WRG 4s".
        tile["light"] = [[px, py, str(t).strip()] for px, py, (t,) in rows["light"]
                         if t and str(t).strip() not in ("-", "")]
    if "sounding" in rows:
        tile["sounding"] = _thin_soundings([[px, py, p[0]] for px, py, p in rows["sounding"]],
                                           64 if z <= 13 else 128)
    if "rock" in rows:
        tile["rock"] = [[px, py, ROCK_TYPES.get(_int(p[0]), 1)] for px, py, p in rows["rock"]]
    if "wreck" in rows:
        tile["wreck"] = [[px, py, WRECK_TYPES.get(_int(p[0]), 1)] for px, py, p in rows["wreck"]]
    if "heritage" in rows:
        tile["heritage"] = _heritage_rows(rows["heritage"], tile.get("wreck", ()), z)
    if "name" in rows:
        tile["name"] = _names(rows["name"], tile, z)


def _heritage_rows(rows: list, wrecks, z: int) -> list:
    """Wrecks, underwater sites and coastal defences:
    [x, y, id, name, class, kind, dating, accuracy, dup, sub-type]. dup 1: a chart wreck is
    drawn at the same spot. Thinned when zoomed out, protected wrecks first."""
    rows = sorted(rows, key=lambda r: (r[2][2] != 0, r[2][3] > 3, r[2][0]))
    out, cells = [], set()
    for px, py, props in rows:
        site, name, cls, kind, dating, acc = props[:6]
        sub = props[6] if len(props) > 6 else None
        if kind == 5 and z < DEFENCE_MIN_Z:
            continue
        if z <= 11:
            cell = (px // 64, py // 64)
            if cell in cells:
                continue
            cells.add(cell)
        dup = 1 if kind <= 3 and z >= 12 and any(abs(wx - px) <= 12 and abs(wy - py) <= 12 for wx, wy, _ in wrecks) else 0
        out.append([px, py, site, name, cls, kind, dating, acc, dup, sub])
    return out


# Name endings that mark islands, islets and capes, or bays and open water, in Finnish and
# Swedish: a fallback for when the feature class doesn't say.
ISLAND_ENDINGS = ("skär", "skäret", "holm", "holmen", "holmarna", "ö", "ön", "öarna", "saari", "saaret",
                  "luoto", "luodot", "kari", "karit", "klobb", "klobben", "kobb", "kobben", "kobbarna",
                  "harun", "hällen", "grund", "grundet", "grynnan", "båda", "bådan", "udd", "udden",
                  "niemi", "näs", "näset", "kallio", "lands", "landet")
WATER_ENDINGS = ("fjärd", "fjärden", "viken", "vik", "selkä", "lahti", "sund", "sundet", "salmi",
                 "fladan", "träsk", "träsket", "järvi", "bukt", "bukten", "strömmen", "ström", "selet")
OPEN_WATER_ENDINGS = ("fjärd", "fjärden", "selkä", "selät")  # big open waters, named sooner
# Coastal towns named on overview charts. The feature classes can't tell towns from villages.
COASTAL_TOWNS = {"Hanko", "Hangö", "Tammisaari", "Ekenäs", "Inkoo", "Ingå", "Kirkkonummi", "Kyrkslätt",
                 "Espoo", "Esbo", "Helsinki", "Helsingfors", "Porvoo", "Borgå", "Loviisa", "Lovisa",
                 "Kotka", "Hamina", "Fredrikshamn", "Paldiski", "Tallinn"}


def _name_kind(code: int | None, text: str = "") -> str:
    """Name class for the panel: s settlement, w water, i island or cape, l other land."""
    low = text.lower()
    if code is not None and 48000 <= code < 49000:
        return "s"
    if (code is not None and 36000 <= code < 37000) or low.endswith(WATER_ENDINGS):
        return "w"
    if code in (35060, 35070) or low.endswith(ISLAND_ENDINGS):
        return "i"
    return "l"


def _name_min_z(code: int | None, kind: str, text: str) -> int:
    """Zoom from which a name shows: big waters and towns first, islands next, inland names
    only up close."""
    if kind == "s":
        return 11 if text in COASTAL_TOWNS else 13
    if text.isupper():
        return 16  # all-capital names are mostly stations, airports and the like
    if kind == "w":
        return 12 if text.lower().endswith(OPEN_WATER_ENDINGS) else 13
    if kind == "i":
        return 13
    return 15


def _water_cells(tile: dict) -> set | None:
    """32-unit cells within about 50 px of the shoreline or a depth line, or None when the
    tile has no chart data to tell by."""
    if "depth" not in tile and "land" not in tile:
        return None
    lo, hi = -TILE_BUFFER, EXTENT + TILE_BUFFER
    marked = set()
    for _, *rings in tile.get("depth", ()):
        for ring in rings:
            n = len(ring)
            for i in range(0, n, 2):
                ax, ay, bx, by = ring[i], ring[i + 1], ring[(i + 2) % n], ring[(i + 3) % n]
                if (ax == bx and ax in (lo, hi)) or (ay == by and ay in (lo, hi)):
                    continue  # the tile's own edge, where an area was cut
                steps = max(abs(bx - ax), abs(by - ay)) // 16 + 1
                for s in range(steps + 1):
                    marked.add((int(ax + (bx - ax) * s / steps) >> 5, int(ay + (by - ay) * s / steps) >> 5))
    return {(cx + dx, cy + dy) for cx, cy in marked for dx in range(-3, 4) for dy in range(-3, 4)}


def _in_water(tile: dict, px: int, py: int) -> bool:
    inside = False
    for _, *rings in tile.get("depth", ()):
        for ring in rings:
            n = len(ring)
            for i in range(0, n, 2):
                ax, ay, bx, by = ring[i], ring[i + 1], ring[(i + 2) % n], ring[(i + 3) % n]
                if (ay > py) != (by > py) and px < (bx - ax) * (py - ay) / (by - ay) + ax:
                    inside = not inside
    return inside


def _names(rows: list, tile: dict, z: int) -> list:
    names = []
    cells = _water_cells(tile) if z < 15 else None
    for px, py, (text, lang, code) in rows:
        text = str(text or "")
        if not text or ("," in text and text.isupper()):
            continue  # e.g. "HANKO, POHJOINEN": station names, not chart names
        code = _int(code)
        kind = _name_kind(code, text)
        if z < _name_min_z(code, kind, text):
            continue
        if cells is not None and kind != "i" and text not in COASTAL_TOWNS and (px >> 5, py >> 5) not in cells:
            # Away from the shore: inland villages, bogs and lakes stay off the sea chart
            # until zoomed right in. Open-water names far out at sea stay.
            if kind != "w" or not _in_water(tile, px, py):
                continue
        names.append([px, py, text, kind, 0 if lang in (None, "fin", "fi") else 1])
    # Finnish first, so where both languages share a spot the panel keeps the Finnish one.
    return [row[:4] for row in sorted(names, key=lambda r: r[4])]


def _int(value: Any) -> int | None:
    v = _num(value)
    return int(v) if v is not None else None


def _thin_soundings(rows: list, cell: int = 64) -> list:
    """Keep the shallowest sounding in each small square, as charts do. The squares line
    up across tiles (cell divides EXTENT and the sounding buffer), so neighbouring tiles
    keep the same soundings near their shared edge and none is cut in half."""
    best: dict[tuple[int, int], list] = {}
    for px, py, depth in rows:
        d = _num(depth)
        if d is None:
            continue
        key = (px // cell, py // cell)
        if key not in best or d < best[key][2]:
            best[key] = [px, py, round(d, 1)]
    return list(best.values())


def _tile_mercator(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = 2 * HALF / 2**z
    minx = -HALF + x * size
    maxy = HALF - y * size
    return minx, maxy - size, minx + size, maxy


def blocks_for(z: int, x: int, y: int) -> list[tuple[int, int]]:
    if z >= BLOCK_Z:
        s = z - BLOCK_Z
        return [(x >> s, y >> s)]
    n = 2 ** (BLOCK_Z - z)
    return [(x * n + i, y * n + j) for j in range(n) for i in range(n)]

class _Skipped(Exception):
    pass


def mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    return x * 180 / HALF, math.degrees(2 * math.atan(math.exp(y * math.pi / HALF)) - math.pi / 2)


def _to_mercator(coords: Any) -> Any:
    """GeoJSON coordinates from lon/lat to web-mercator metres, at any nesting depth."""
    if coords and isinstance(coords[0], (int, float)):
        return lonlat_to_mercator(coords[0], coords[1])
    return [_to_mercator(c) for c in coords]


def _mml_features(page: dict) -> list[dict]:
    """MML features with lon/lat geometry converted, and the name text under one key."""
    out = []
    for f in page.get("features") or []:
        geom = f.get("geometry")
        if not geom or not geom.get("coordinates"):
            continue
        props = f.get("properties") or {}
        text = props.get("teksti") or props.get("nimi") or props.get("teksti_fin") or props.get("name")
        if isinstance(text, dict):  # a text with its language
            text = text.get("teksti") or text.get("text") or next(iter(text.values()), None)
        out.append({"geometry": {"type": geom["type"], "coordinates": _to_mercator(geom["coordinates"])},
                    "properties": {**props, "teksti": text}})
    return out



class VectorChart:
    def __init__(self, directory: Path, session: aiohttp.ClientSession | None,
                 fetch_layer: Callable[[str, tuple[float, float, float, float]], Any] | None = None,
                 mml_key: str = ""):
        self.dir = directory
        self.session = session
        self.fetch_layer = fetch_layer  # None: the real services, a page at a time
        self.mml_key = mml_key
        self._memory: OrderedDict[tuple[int, int, int], tuple[dict, int]] = OrderedDict()
        self._memory_bytes = 0
        self._pending: dict[tuple[int, int, int], asyncio.Task] = {}
        self._fetching: dict[tuple, asyncio.Task] = {}  # part downloads and conversions in flight
        self._wanted: dict[tuple[int, int], float] = {}  # when a block was last asked for
        self._limit = asyncio.Semaphore(1)  # one download at a time: gentle on the services and on RAM
        self._reading = asyncio.Semaphore(2)  # block files read at once
        self._cutting = asyncio.Semaphore(1)
        self._failed: dict[tuple, float] = {}
        self._missing: dict[Path, tuple[float, bool]] = {}  # file -> (mtime, some layer failed)
        self._regions: OrderedDict[tuple[int, int], Layer] = OrderedDict()  # heritage, mercator metres
        self._coast: OrderedDict[tuple[int, int], tuple] = OrderedDict()  # block -> (region, coastal sites)
        self._logged_props: set[str] = set()
        self.errors: dict[str, str] = {}  # layer -> last error, for the panel
        self.blocks_fetched = 0
        self.keep_status: dict[str, Any] | None = None  # progress of keep(), None while it's off

    def set_mml_key(self, key: str) -> None:
        """A new Land Survey key: blocks are read again so rocks and names get fetched."""
        self.mml_key = key.strip()
        self._forget()
        self._failed = {k: t for k, t in self._failed.items() if k[-1] != "mml"}
        for layer in PARTS["mml"]:
            self.errors.pop(layer, None)

    def _forget(self) -> None:
        self._memory.clear()
        self._memory_bytes = 0

    async def check_mml_key(self, key: str) -> str | None:
        """None if the Land Survey accepts the key, else what went wrong."""
        if self.session is None:
            return None
        try:
            await self._get_json(f"{MML_API}/collections/vesikivi/items",
                                 {"bbox": "24.40,59.98,24.42,59.99", "limit": "1", "api-key": key.strip()}, "rock")
        except aiohttp.ClientError as err:
            return str(err)
        except (asyncio.TimeoutError, ValueError) as err:
            return str(err) or type(err).__name__
        return None

    def _parts(self) -> list[str]:
        return [part for part in PARTS if part != "mml" or self.mml_key]

    def _path(self, bx: int, by: int, part: str = "", suffix: str = SUFFIX) -> Path:
        return self.dir / "blocks" / f"{bx}_{by}{'.' + part if part else ''}{suffix}"

    def status(self) -> dict[str, Any]:
        root = self.dir / "blocks"
        files = list(root.iterdir()) if root.exists() else []
        blocks = {f.name.split(".")[0] for f in files if f.name.endswith((SUFFIX, OLD_SUFFIX))}
        return {"blocks": len(blocks),
                "mb": round(sum(f.stat().st_size for f in files if f.is_file()) / 1e6, 1),
                "errors": self.errors, "attribution": ATTRIBUTION, "min_zoom": MIN_Z, "keep": self.keep_status,
                "mml_key": bool(self.mml_key), "memory_mb": round(self._memory_bytes / 1e6, 1),
                "rev": f"{TILE_REV}{''.join(p[:1] for p in self._parts())}"}

    def _fresh(self, key: tuple[int, int]) -> bool:
        for part in self._parts():
            age = _age(self._path(*key, part))
            if age is None or age >= MAX_AGE or self._path(*key, part, OLD_SUFFIX).exists():
                return False
        return True

    async def keep(self, blocks: list[tuple[int, int]] | None = None) -> None:
        """Download every block of KEEP_AREA to disk, one at a time, and keep them fresh.

        Runs until cancelled. Panel requests still go first: they queue for the same
        one-download-at-a-time lock, and this only asks for its next block after the
        previous one is done. Nothing is read into memory here.
        """
        blocks = keep_blocks() if blocks is None else blocks
        try:
            while True:
                done = sum(self._fresh(key) for key in blocks)
                self.keep_status = {"total": len(blocks), "done": done, "running": done < len(blocks)}
                for key in blocks:
                    await self._ensure_heritage(*key)
                    if self._fresh(key):
                        continue
                    self._wanted[key] = math.inf  # never skipped as "the map moved on"
                    ok = True
                    for part in self._parts():
                        ok = await self._ensure_part(*key, part) and ok
                    self.keep_status["done"] = sum(self._fresh(k) for k in blocks)
                    # A failed download backs off, so a service that's down isn't hammered.
                    await asyncio.sleep(KEEP_PAUSE if ok and self._fresh(key) else 60)
                self.keep_status["running"] = False
                await asyncio.sleep(KEEP_RECHECK)
        finally:
            self.keep_status = None

    async def tile(self, z: int, x: int, y: int,
                   wait: float | None = None) -> tuple[dict[str, Any], bool] | None:
        """The tile, and whether every block it needs was available.

        With ``wait``, gives up after that many seconds and returns None while the
        blocks keep downloading in the background: a browser only opens a few
        connections to Home Assistant, and a tile request held open for half a
        minute would stall the rest of the panel (and Home Assistant's own UI).
        """
        keys = blocks_for(z, x, y)
        level = level_for(z)
        now = time.monotonic()
        if len(self._wanted) > 1024:
            self._wanted = {k: t for k, t in self._wanted.items() if now - t < FORGET_AFTER}
        for key in keys:
            self._wanted[key] = now
        tasks = [self._block_task(key, level) for key in keys]
        regions = {self._region(*key) for key in keys} if z >= LAYER_MIN_Z["heritage"] else set()
        heritage = [self._ensure_heritage_task(*r) for r in regions]
        pending = [t for t in tasks + heritage if not t.done()]
        if pending:
            done, still = await asyncio.wait([asyncio.shield(t) for t in pending], timeout=wait)
            if still:
                return None
        loaded = [t.result() for t in tasks]
        regions_ok = all(t.result() for t in heritage)
        blocks = {}
        for key, data in zip(keys, loaded):
            if data is not None and z >= LAYER_MIN_Z["heritage"]:
                data = {**data, "heritage": await self._coastal_sites(key, data)}
            blocks[key] = data
        async with self._cutting:  # one tile cut at a time keeps the web server responsive
            tile = await asyncio.to_thread(make_tile, z, x, y, blocks)
        return tile, regions_ok and all(b is not None and b["_complete"] for b in loaded)

    async def block(self, bx: int, by: int, level: int = len(LEVELS) - 1) -> dict | None:
        self._wanted[(bx, by)] = time.monotonic()
        return await asyncio.shield(self._block_task((bx, by), level))

    def _block_task(self, key: tuple[int, int], level: int) -> "asyncio.Future[dict | None]":
        mkey = (*key, level)
        if mkey in self._memory:
            self._memory.move_to_end(mkey)
            future = asyncio.get_running_loop().create_future()
            future.set_result(self._memory[mkey][0])
            return future
        task = self._pending.get(mkey)
        if task is None:
            task = asyncio.create_task(self._load(*key, level))
            self._pending[mkey] = task
            task.add_done_callback(lambda _t, mkey=mkey: self._pending.pop(mkey, None))
        return task

    def _stale(self, key: tuple[int, int]) -> bool:
        return time.monotonic() - self._wanted.get(key, time.monotonic()) > FORGET_AFTER

    async def _load(self, bx: int, by: int, level: int) -> dict | None:
        parts = self._parts()
        have = []
        try:
            for part in parts:
                if await self._ensure_part(bx, by, part):
                    have.append(part)
                elif part == "":
                    return None  # without depths there's no chart to draw
        except _Skipped:
            return None
        async with self._reading:
            if self._stale((bx, by)):
                return None  # the map moved on while this block waited its turn
            layers = await asyncio.to_thread(self._read_parts, bx, by, have, level)
        if layers is None:
            return None
        layers["_complete"] = len(have) == len(parts)
        if layers["_complete"]:
            self._remember((bx, by, level), layers)
        return layers

    def _read_parts(self, bx: int, by: int, parts: list[str], level: int) -> dict | None:
        layers: dict[str, Any] = {}
        for part in parts:
            path = self._path(bx, by, part)
            try:
                meta, got = read_block(path, level)
            except (OSError, ValueError, EOFError) as err:
                _LOGGER.warning("Chart block file %s unreadable, fetched again later: %s", path.name, err)
                try:
                    path.unlink()
                except OSError:
                    pass
                if part == "":
                    return None
                continue
            self._missing[path] = (path.stat().st_mtime, bool(meta.get("missing")))
            layers.update(got)
        return layers

    def _remember(self, key: tuple[int, int, int], layers: dict) -> None:
        size = sum(L.nbytes() for k, L in layers.items() if k[0] != "_")
        old = self._memory.pop(key, None)
        if old:
            self._memory_bytes -= old[1]
        self._memory[key] = (layers, size)
        self._memory_bytes += size
        while len(self._memory) > 1 and (self._memory_bytes > MEMORY_BYTES or len(self._memory) > MEMORY_BLOCKS):
            _, (_, dropped) = self._memory.popitem(last=False)
            self._memory_bytes -= dropped

    # --- parts on disk ---

    async def _ensure_part(self, bx: int, by: int, part: str) -> bool:
        """Make sure a block part is on disk (fetching or converting it if need be). True if
        a copy is there, even an old one after a failed download."""
        key = (bx, by, part)
        task = self._fetching.get(key)
        if task is None:
            task = asyncio.create_task(self._ensure(bx, by, part))
            self._fetching[key] = task
            task.add_done_callback(lambda _t, key=key: self._fetching.pop(key, None))
        return await asyncio.shield(task)

    async def _ensure(self, bx: int, by: int, part: str) -> bool:
        path = self._path(bx, by, part)
        old = self._path(bx, by, part, OLD_SUFFIX)
        if old.exists():
            await self._convert(old, path)
        age = _age(path)
        if age is not None and age < MAX_AGE and not (age > FAILED_RETRY and await self._has_missing(path)):
            return True
        if time.time() - self._failed.get((bx, by, part), 0) < FAILED_RETRY:
            return age is not None  # an old copy beats none
        if await self._fetch_part(bx, by, part):
            return True
        self._failed[(bx, by, part)] = time.time()
        return age is not None

    async def _has_missing(self, path: Path) -> bool:
        """Whether some layer of a part failed when it was fetched (it's then fetched again sooner)."""
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return False
        known = self._missing.get(path)
        if known is None or known[0] != mtime:
            try:
                meta = await asyncio.to_thread(read_meta, path)
            except (OSError, ValueError, EOFError):
                return True
            known = self._missing[path] = (mtime, bool(meta.get("missing")))
        return known[1]

    async def _convert(self, old: Path, path: Path) -> None:
        """Rewrite a block file from the earlier JSON format, keeping its age."""
        try:
            async with self._reading:
                mtime = old.stat().st_mtime
                meta, levels = await asyncio.to_thread(convert_old, old)
                await asyncio.to_thread(write_block, path, meta, levels)
                os.utime(path, (mtime, mtime))
                del levels
        except FileNotFoundError:
            return
        except (OSError, ValueError, EOFError) as err:
            _LOGGER.warning("Could not convert chart block %s, fetching it again: %s", old.name, err)
        try:
            old.unlink()
        except OSError:
            pass

    async def _fetch_part(self, bx: int, by: int, part: str) -> bool:
        bounds = block_bounds(bx, by)
        size = bounds[2] - bounds[0]
        keys = PARTS[part]
        built: dict[str, Layer] = {}
        missing: list[str] = []
        if not near_finland(bounds):
            built = {key: Layer(SOURCES[key][2]) for key in keys}  # nothing to fetch out there
        async with self._limit:
            if not built and self._stale((bx, by)):
                raise _Skipped  # the map moved on while this block waited its turn
            started = time.monotonic()
            for key in keys if not built else ():
                # Points reach further past the block than lines and areas need to.
                pad = BLOCK_POINT_MARGIN / UNITS * size if SOURCES[key][2] == "point" else size * TILE_BUFFER / EXTENT
                bbox = (bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad)
                builder = LayerBuilder(SOURCES[key][2])
                try:
                    await self._fetch_into(builder, key, bx, by, bbox)
                    self.errors.pop(key, None)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as err:
                    msg = str(err) or type(err).__name__
                    if self.errors.get(key) != msg:
                        _LOGGER.warning("Chart data %s for block %s,%s failed: %s", key, bx, by, msg)
                    self.errors[key] = msg
                    if key == "depth":
                        return False  # without depths there's no chart to draw
                    builder = LayerBuilder(SOURCES[key][2])
                    missing.append(key)
                built[key] = builder.layer
            if part and len(missing) == len(keys):
                return False  # nothing of this part came through; tried again later
        path = self._path(bx, by, part)
        levels = await asyncio.to_thread(make_levels, built)
        await asyncio.to_thread(write_block, path, {"v": 2, "missing": missing}, levels)
        self._missing[path] = (path.stat().st_mtime, bool(missing))
        self.blocks_fetched += 1
        _LOGGER.info("Chart block %s,%s%s: %s in %.1f s", bx, by, f" ({part})" if part else "",
                     ", ".join(f"{L.n} {k}" for k, L in built.items()), time.monotonic() - started)
        return True

    async def _fetch_into(self, builder: LayerBuilder, key: str, bx: int, by: int,
                          bbox: tuple[float, float, float, float]) -> None:
        """Fetch one layer for a block, building each page into the layer as it comes so
        only one page of raw data is ever in memory."""
        def add(features: list[dict]) -> None:
            if features and key not in self._logged_props:
                # Logged once, to show which fields a service actually returns.
                self._logged_props.add(key)
                _LOGGER.info("Chart data %s fields: %s", key, ", ".join(sorted(features[0].get("properties") or {})))
            build_into(builder, bx, by, key, features)

        if self.fetch_layer is not None:
            features = await self.fetch_layer(key, bbox)
            await asyncio.to_thread(add, features)
        elif SOURCES[key][0] == MML_API:
            await self._fetch_mml(key, bbox, add)
        else:
            await self._fetch_wfs(key, bbox, add)

    async def _get_json(self, url: str, params: dict[str, str] | None, what: str) -> dict:
        async with self.session.get(url, params=params, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=120)) as resp:
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise _TooBig(f"more than {MAX_RESPONSE // 1048576} MB of {what} data for one area")
                chunks.append(chunk)
            body = b"".join(chunks)
            del chunks
            if resp.status != 200 or not body.lstrip().startswith(b"{"):
                raise aiohttp.ClientError(
                    f"HTTP {resp.status}: {body[:200].decode('utf-8', 'replace').strip()}")
        return await asyncio.to_thread(json.loads, body)  # off the event loop

    async def _fetch_wfs(self, key: str, bbox: tuple[float, float, float, float],
                         sink: Callable[[list[dict]], None] | None = None) -> list[dict]:
        """A WFS layer in a bbox, a page at a time. Each page goes to ``sink`` (in a
        thread) and is then dropped; without a sink the features are returned."""
        url, type_name, _, props = SOURCES[key]
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": type_name,
            "srsName": "EPSG:3857", "outputFormat": "application/json",
            "bbox": ",".join(f"{v:.1f}" for v in bbox) + ",EPSG:3857",
        }
        if url == VAYLA_WFS:
            params["propertyName"] = ",".join((*props, "geom"))
        return await self._paged(url, params, key, sink)

    async def _paged(self, url: str, params: dict[str, str], what: str,
                     sink: Callable[[list[dict]], None] | None) -> list[dict]:
        features: list[dict] = []
        start, count, got_total = 0, PAGE, 0
        while True:
            params["startIndex"] = str(start)
            params["count"] = str(count)
            try:
                page = await self._get_json(url, params, what)
            except _TooBig:
                if count <= 25:
                    raise
                count //= 2  # dense area: the same page again, in halves
                continue
            got = page.get("features") or []
            got_total += len(got)
            if sink is None:
                features += got
            else:
                await asyncio.to_thread(sink, got)
            total = page.get("numberMatched", page.get("totalFeatures"))
            del page
            if isinstance(total, int):
                if not got or got_total >= total:
                    return features
            elif len(got) < count:
                return features
            start += len(got)
            del got

    async def _fetch_mml(self, key: str, bbox: tuple[float, float, float, float],
                         sink: Callable[[list[dict]], None]) -> None:
        """An MML OGC API Features collection in a bbox, following its next-page links."""
        _, collection, _, _ = SOURCES[key]
        lon0, lat0 = mercator_to_lonlat(bbox[0], bbox[1])
        lon1, lat1 = mercator_to_lonlat(bbox[2], bbox[3])
        url: str | None = f"{MML_API}/collections/{collection}/items"
        params: dict[str, str] | None = {"bbox": f"{lon0:.6f},{lat0:.6f},{lon1:.6f},{lat1:.6f}",
                                         "limit": str(MML_PAGE), "api-key": self.mml_key}
        for _ in range(500):
            page = await self._get_json(url, params, key)
            await asyncio.to_thread(lambda p=page: sink(_mml_features(p)))
            nxt = next((link.get("href") for link in page.get("links") or []
                        if link.get("rel") == "next" and link.get("href")), None)
            if not nxt or not page.get("features"):
                break
            url, params = nxt, None
            if "api-key=" not in url:
                url += ("&" if "?" in url else "?") + f"api-key={self.mml_key}"

    # --- heritage sites ---

    @staticmethod
    def _region(bx: int, by: int) -> tuple[int, int]:
        s = BLOCK_Z - HERITAGE_Z
        return bx >> s, by >> s

    def _ensure_heritage_task(self, rx: int, ry: int) -> "asyncio.Future[bool]":
        if (rx, ry) in self._regions:
            self._regions.move_to_end((rx, ry))
            future = asyncio.get_running_loop().create_future()
            future.set_result(True)
            return future
        key = ("heritage", rx, ry)
        task = self._fetching.get(key)
        if task is None:
            task = asyncio.create_task(self._load_region(rx, ry))
            self._fetching[key] = task
            task.add_done_callback(lambda _t, key=key: self._fetching.pop(key, None))
        return task

    async def _ensure_heritage(self, bx: int, by: int) -> bool:
        return await asyncio.shield(self._ensure_heritage_task(*self._region(bx, by)))

    async def _load_region(self, rx: int, ry: int) -> bool:
        """Heritage sites of a zoom-7 region into memory, fetched once a month."""
        path = self.dir / "heritage" / f"{rx}_{ry}.v2.json.gz"  # v2 adds coastal defences
        age = _age(path)
        rows = None
        failed = time.time() - self._failed.get(("heritage", rx, ry), 0) < FAILED_RETRY
        if (age is None or age >= MAX_AGE) and not failed:
            rows = await self._fetch_region(rx, ry)
            if rows is None:
                self._failed[("heritage", rx, ry)] = time.time()
            else:
                try:
                    await asyncio.to_thread(_write_json, path, {"v": 2, "rows": rows})
                except OSError as err:
                    _LOGGER.warning("Could not store heritage sites: %s", err)
        if rows is None and age is not None:
            try:
                rows = (await asyncio.to_thread(_read_json, path))["rows"]
            except (OSError, ValueError, KeyError):
                rows = None
        if rows is None:
            return False
        b = LayerBuilder("point")
        for x, y, *props in rows:
            b.point(props, x, y)
        self._regions[(rx, ry)] = b.layer
        while len(self._regions) > HERITAGE_REGIONS:
            self._regions.popitem(last=False)
        return True

    async def _fetch_region(self, rx: int, ry: int) -> list | None:
        minx, miny, maxx, maxy = _tile_mercator(HERITAGE_Z, rx, ry)
        if not near_finland((minx, miny, maxx, maxy)):
            return []
        bbox = (minx, miny, maxx, maxy)
        async with self._limit:
            try:
                if self.fetch_layer is not None:
                    features = await self.fetch_layer("heritage", bbox)
                else:
                    features = await self._fetch_heritage(bbox)
                self.errors.pop("heritage", None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as err:
                msg = str(err) or type(err).__name__
                if self.errors.get("heritage") != msg:
                    _LOGGER.warning("Heritage sites for region %s,%s failed: %s", rx, ry, msg)
                self.errors["heritage"] = msg
                return None
        rows = []
        for f in features:
            props = heritage_props(f)
            pts = _points(f.get("geometry") or {"type": ""})
            if props and pts:
                rows.append([round(pts[0][0]), round(pts[0][1]), *props])
        _LOGGER.info("Heritage sites for region %s,%s: %s", rx, ry, len(rows))
        return rows

    async def _fetch_heritage(self, bbox: tuple[float, float, float, float]) -> list[dict]:
        """Underwater sites, wrecks and defence sites from each of Museovirasto's register
        layers. Defences come for land too; the tiles keep only those near the sea."""
        x0, y0, x1, y1 = bbox
        cql = (f"(vedenalainen ILIKE 'k' OR tyyppi ILIKE '%hyly%' OR tyyppi ILIKE '%puolustus%') AND "
               f"BBOX(Shape,{x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f},'EPSG:3857')")
        features: list[dict] = []
        failed: list[str] = []
        for type_name, cls in MV_TYPES.items():
            params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": type_name,
                      "srsName": "EPSG:3857", "outputFormat": "application/json", "sortBy": "OBJECTID",
                      "propertyName": ",".join(("Shape", *MV_FIELDS)), "CQL_FILTER": cql}
            try:
                try:
                    got = await self._paged(MV_WFS, params, "heritage", None)
                except aiohttp.ClientError as err:
                    if not str(err).startswith("HTTP"):
                        raise
                    # A layer without one of the fields refuses the field list: ask for everything.
                    del params["propertyName"]
                    got = await self._paged(MV_WFS, params, "heritage", None)
            except (aiohttp.ClientError, ValueError) as err:
                failed.append(f"{type_name.split(':')[-1]}: {err}")
                continue
            for f in got:
                f.setdefault("properties", {})["_class"] = cls
                features.append(f)
        if failed:
            if len(failed) == len(MV_TYPES):
                raise aiohttp.ClientError("; ".join(failed))
            _LOGGER.info("Some heritage layers were left out: %s", "; ".join(failed))
        return features

    async def _coastal_sites(self, key: tuple[int, int], data: dict) -> Layer:
        """A block's heritage sites with inland defences left out, worked out once per block
        (and again when its region is reloaded)."""
        region = self._regions.get(self._region(*key))
        hit = self._coast.get(key)
        if hit is not None and hit[0] is region:
            self._coast.move_to_end(key)
            return hit[1]
        layer = await asyncio.to_thread(coastal_filter, self._heritage_layer(*key), data.get("depth"))
        self._coast[key] = (region, layer)
        while len(self._coast) > MEMORY_BLOCKS:
            self._coast.popitem(last=False)
        return layer

    def _heritage_layer(self, bx: int, by: int) -> Layer:
        """A block's heritage sites (block units, with the usual point margin)."""
        region = self._regions.get(self._region(bx, by))
        b = LayerBuilder("point")
        if region is None:
            return b.layer
        minx, miny, maxx, maxy = block_bounds(bx, by)
        scale = UNITS / (maxx - minx)
        m = BLOCK_POINT_MARGIN
        co = region.co
        for i in range(region.n):
            x, y = (co[2 * i] - minx) * scale, (maxy - co[2 * i + 1]) * scale
            if -m <= x <= UNITS + m and -m <= y <= UNITS + m:
                b.point(region.props[region.pi[i]], x, y)
        return b.layer

    def prune(self) -> None:
        """Drop blocks nobody has looked at for two refresh periods."""
        for root in (self.dir / "blocks", self.dir / "heritage"):
            if not root.exists():
                continue
            for p in root.iterdir():
                try:
                    if time.time() - p.stat().st_mtime > 2 * MAX_AGE:
                        p.unlink()
                except FileNotFoundError:
                    pass


class _TooBig(ValueError):
    pass


def _age(path: Path) -> float | None:
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return None


def _read_json(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)
