"""A made-up stretch of coast like Porkkala's, for the simulator (FW_SIMULATE=1).

The simulator's islands are circles; this is a few kilometres of coast drawn from a
smooth random height field instead, so the drawn chart can be compared with the paper
chart: a mainland, a long peninsula with a narrow bay, skerries and shoals, depth areas
and contours traced from the field, soundings, rocks, land contours, roads, houses,
names, a boat route with its marks and a leading line, and a cable across the bay.
Never used in a normal install. Pure Python, made once and kept (a few MB).
"""

from __future__ import annotations

import math
import random
from functools import lru_cache

HALF = 20037508.342789244

# The box (lon, lat), west of the simulator's islands. Its top edge meets the simulator's
# mainland exactly.
LON0, LON1, LAT0, LAT1 = 23.96, 24.20, 59.96, 60.10
CELL = 80.0  # web-mercator metres per grid cell (about 40 m on the ground)


def merc(lon: float, lat: float) -> tuple[float, float]:
    return lon * HALF / 180, math.log(math.tan((90 + lat) * math.pi / 360)) * HALF / math.pi


X0, Y0 = merc(LON0, LAT0)
X1, Y1 = merc(LON1, LAT1)
NX = int((X1 - X0) / CELL) + 1
NY = int((Y1 - Y0) / CELL) + 1
GROUND = 0.5  # ground metres per web-mercator metre at 60°N


def _km(lon: float, lat: float) -> tuple[float, float]:
    """Ground km east and north of the box's south-west corner."""
    x, y = merc(lon, lat)
    return (x - X0) * GROUND / 1000, (y - Y0) * GROUND / 1000


def _lonlat(kx: float, ky: float) -> tuple[float, float]:
    x, y = X0 + kx * 1000 / GROUND, Y0 + ky * 1000 / GROUND
    return x * 180 / HALF, math.degrees(2 * math.atan(math.exp(y * math.pi / HALF)) - math.pi / 2)


class _Noise:
    """Smooth value noise with a lattice every `step` km."""

    def __init__(self, seed: int, step: float):
        self.rnd = random.Random(seed)
        self.step = step
        self.cache: dict[tuple[int, int], float] = {}

    def _at(self, i: int, j: int) -> float:
        v = self.cache.get((i, j))
        if v is None:
            v = self.cache[(i, j)] = self.rnd.uniform(-1, 1)
        return v

    def __call__(self, x: float, y: float) -> float:
        fx, fy = x / self.step, y / self.step
        i, j = math.floor(fx), math.floor(fy)
        tx, ty = fx - i, fy - j
        sx, sy = tx * tx * (3 - 2 * tx), ty * ty * (3 - 2 * ty)
        a = self._at(i, j) + (self._at(i + 1, j) - self._at(i, j)) * sx
        b = self._at(i, j + 1) + (self._at(i + 1, j + 1) - self._at(i, j + 1)) * sx
        return a + (b - a) * sy


# Hand-placed things, in ground km from the south-west corner (the box is about 13.4 km
# wide and 15.6 km tall).
TIP_Y = 3.7  # the peninsula's southern tip
BAY = (7.4, 3.9, 8.6)  # the bay's x, mouth and head y
SKERRIES = [(10.2, 5.6, 0.45, 9), (11.4, 6.6, 0.3, 6), (9.6, 3.9, 0.35, 7), (12.1, 4.6, 0.5, 11),
            (10.9, 3.1, 0.25, 5), (4.2, 3.2, 0.3, 6), (3.1, 9.6, 0.35, 7), (2.4, 11.4, 0.25, 4),
            (8.9, 10.8, 0.3, 6), (12.4, 9.4, 0.4, 8), (5.1, 6.4, 0.18, 3), (11.0, 8.2, 0.2, 4)]
# The boat route meets the leading line south of its front mark and follows it out to sea.
BOAT_ROUTE = [(0.4, 2.0), (4.6, 2.6), (7.425, 2.2), (7.5, 0.8)]
LEAD = [(7.2, 9.6), (7.35, 3.6), (7.5, 0.8)]  # rear mark, front mark, out to sea
CABLE = [(0.0, 7.3), (2.6, 6.9), (5.0, 7.7), (6.2, 9.0)]


@lru_cache(maxsize=1)
def _small_skerries() -> list[tuple[float, float, float, float]]:
    """Islets and shoals strewn over the archipelago east and south of the peninsula."""
    rnd = random.Random(9)
    out = []
    while len(out) < 70:
        x, y = rnd.uniform(0.5, 13), rnd.uniform(0.8, 12)
        if (x > 8.4 or y < 4.5 or (x < 4.5 and rnd.random() < 0.35)) and abs(x - 6.8) > 1.3:
            out.append((x, y, rnd.uniform(0.06, 0.22), rnd.uniform(-4, 5)))
    return out


def _peninsula_half_width(y: float) -> float:
    # Wide where it leaves the mainland, narrowing to the tip, with a bulge (the "landet").
    return 1.0 + 0.6 * math.exp(-((y - 7.0) / 1.6) ** 2) + 0.35 * max(0.0, (y - 9.5) / 3)


def _seg_dist(px: float, py: float, line) -> float:
    best = math.inf
    for (ax, ay), (bx, by) in zip(line, line[1:]):
        vx, vy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)))
        best = min(best, math.hypot(px - ax - t * vx, py - ay - t * vy))
    return best


def _field() -> list[list[float]]:
    """Height (m, + land, - depth) on the grid, rows from north to south, padded: a ring
    of very deep cells round it, and above the top row (the mainland) a land row first,
    so contours close along the box edges."""
    n1, n2, n3 = _Noise(3, 1.4), _Noise(5, 0.55), _Noise(7, 0.22)
    w = (X1 - X0) * GROUND / 1000
    h = (Y1 - Y0) * GROUND / 1000
    rows = []
    for r in range(NY):
        ky = h - r * CELL * GROUND / 1000
        row = []
        for c in range(NX):
            kx = c * CELL * GROUND / 1000
            f = -26 - 9 * max(0.0, (4.0 - ky) / 4.0)
            f += 70 / (1 + math.exp(-(ky - 12.6) / 0.5))  # the mainland
            hw = _peninsula_half_width(ky)
            f += 62 * math.exp(-((kx - 6.8 - 0.25 * math.sin(ky)) / hw) ** 2) / (1 + math.exp(-(ky - TIP_Y) / 0.35))
            for sx, sy, sr, sh in SKERRIES + _small_skerries():
                f = max(f, -30 + (sh + 30) * math.exp(-((kx - sx) ** 2 + (ky - sy) ** 2) / (2.2 * sr) ** 2))
            noise = 9 * n1(kx, ky) + 6 * n2(kx, ky) + 3 * n3(kx, ky)
            f += noise
            # The bay: narrow and 4-6 m deep, with the boat route dredged to 3 m.
            bx, by0, by1 = BAY
            if by0 - 1.2 < ky < by1 + 0.3:
                inside = math.exp(-((kx - bx - 0.15 * math.sin(2 * ky)) / 0.34) ** 2)
                fade = min(1.0, (ky - by0 + 1.2) / 1.2, (by1 + 0.3 - ky) / 0.3)
                f = f * (1 - inside * fade) + min(f, -5.5 + 2 * n3(kx, ky)) * inside * fade
            d = _seg_dist(kx, ky, BOAT_ROUTE)
            if d < 0.12:
                f = min(f, -3.2)
            # Open sea at the side and south edges, land along the top.
            edge = min(kx, w - kx, ky)
            if edge < 0.8:
                f = min(f, -14 + (f + 14) * edge / 0.8) if f > -14 else f
            if h - ky < 0.6:
                f = max(f, 6 + 4 * (0.6 - (h - ky)))
            row.append(f)
        rows.append(row)
    deep = -1000.0
    padded = [[deep] * (NX + 2), [deep] + [60.0] * NX + [deep]]
    padded += [[deep] + row + [deep] for row in rows]
    padded.append([deep] * (NX + 2))
    return padded


def _loops(f: list[list[float]], level: float) -> list[list[tuple[float, float]]]:
    """Closed contour loops of f at level, in (col, row) grid units of the padded grid."""
    nr, nc = len(f), len(f[0])
    nbr: dict = {}

    def add(a, b):
        nbr.setdefault(a, []).append(b)
        nbr.setdefault(b, []).append(a)

    for r in range(nr - 1):
        top, bot = f[r], f[r + 1]
        for c in range(nc - 1):
            k = (top[c] > level) * 8 + (top[c + 1] > level) * 4 + (bot[c + 1] > level) * 2 + (bot[c] > level)
            if k == 0 or k == 15:
                continue
            T, R, B, L = ("h", r, c), ("v", r, c + 1), ("h", r + 1, c), ("v", r, c)
            if k in (1, 14):
                add(L, B)
            elif k in (2, 13):
                add(B, R)
            elif k in (3, 12):
                add(L, R)
            elif k in (4, 11):
                add(T, R)
            elif k in (6, 9):
                add(T, B)
            elif k in (7, 8):
                add(L, T)
            else:
                centre = (top[c] + top[c + 1] + bot[c] + bot[c + 1]) / 4 > level
                if (k == 5) == centre:
                    add(L, T)
                    add(B, R)
                else:
                    add(T, R)
                    add(L, B)

    def point(key):
        kind, r, c = key
        if kind == "h":
            a, b = f[r][c], f[r][c + 1]
            return (c + (level - a) / (b - a), r)
        a, b = f[r][c], f[r + 1][c]
        return (c, r + (level - a) / (b - a))

    seen = set()
    out = []
    for start in nbr:
        if start in seen:
            continue
        loop, prev, cur = [start], None, start
        seen.add(start)
        while True:
            a, b = nbr[cur][0], nbr[cur][1]
            nxt = b if a == prev else a
            if nxt == start or nxt in seen:
                break
            loop.append(nxt)
            seen.add(nxt)
            prev, cur = cur, nxt
        if len(loop) >= 5:
            out.append(_chaikin([point(k) for k in loop]))
    return out


def _chaikin(pts):
    out = []
    for i in range(len(pts)):
        (ax, ay), (bx, by) = pts[i], pts[(i + 1) % len(pts)]
        out += [(0.75 * ax + 0.25 * bx, 0.75 * ay + 0.25 * by), (0.25 * ax + 0.75 * bx, 0.25 * ay + 0.75 * by)]
    return out


def _to_merc(pt) -> list[float]:
    c, r = pt
    return [X0 + (c - 1) * CELL, Y1 - (r - 2) * CELL]


def _clip_box(ring):
    """A ring (mercator) clipped to the box, closed."""
    from .vchart import clip_ring
    out = clip_ring([tuple(p) for p in ring], X0, Y0, X1, Y1)
    return [list(p) for p in out] + [list(out[0])] if len(out) >= 3 else None


def _open_lines(loop):
    """A contour loop as lines, leaving out its stretches along the box's edges."""
    lines, cur = [], []
    for p in loop + loop[:1]:
        x, y = p
        if X0 + 1 < x < X1 - 1 and Y0 + 1 < y < Y1 - 1:
            cur.append(p)
        else:
            if len(cur) >= 2:
                lines.append(cur)
            cur = []
    if len(cur) >= 2:
        lines.append(cur)
    return lines


def _poly(rings, **props):
    return {"geometry": {"type": "Polygon", "coordinates": rings}, "properties": props}


def _line(points, **props):
    return {"geometry": {"type": "LineString", "coordinates": points}, "properties": props}


def _point(x, y, **props):
    return {"geometry": {"type": "Point", "coordinates": [x, y]}, "properties": props}


def _km_line(points):
    return [list(merc(*_lonlat(x, y))) for x, y in points]


def _house(x, y, rnd) -> list[list[float]]:
    """A building's outline round (x, y) in web-mercator metres: a turned rectangle of a
    house's size, now and then an L-shaped or a big one, as the Land Survey has them."""
    big = rnd.random() < 0.06
    w, h = (rnd.uniform(40, 110), rnd.uniform(24, 60)) if big else (rnd.uniform(14, 30), rnd.uniform(12, 22))
    if big or rnd.random() < 0.2:
        pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, 0), (0, 0), (0, h / 2), (-w / 2, h / 2)]
    else:
        pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    a = rnd.uniform(0, math.pi)
    ring = [[x + px * math.cos(a) - py * math.sin(a), y + px * math.sin(a) + py * math.cos(a)] for px, py in pts]
    return ring + ring[:1]


def _km_point(x, y, **props):
    return _point(*merc(*_lonlat(x, y)), **props)


@lru_cache(maxsize=1)
def scene() -> dict[str, list[dict]]:
    """Every layer's features (web-mercator GeoJSON-like, as the WFS services give them)."""
    f = _field()
    loops = {lv: [[_to_merc(p) for p in loop] for loop in _loops(f, lv)] for lv in (0, -3, -6, -10, -20)}
    out: dict[str, list[dict]] = {}

    def clipped(level):
        return [r for r in (_clip_box(lp) for lp in loops[level]) if r]

    # The deep water reaches a little past the box (west, south and east), over the
    # simulator's own, so no seam shows where they meet.
    m = 600
    box = [[X0 - m, Y0 - m], [X1 + m, Y0 - m], [X1 + m, Y1], [X0 - m, Y1], [X0 - m, Y0 - m]]
    out["depth"] = [
        _poly(clipped(-3) + clipped(0), DRVAL1=0, DRVAL2=3),
        _poly(clipped(-6) + clipped(-3), DRVAL1=3, DRVAL2=6),
        _poly(clipped(-10) + clipped(-6), DRVAL1=6, DRVAL2=10),
        _poly([box] + clipped(-10), DRVAL1=10, DRVAL2=40),
    ]
    out["contour"] = [_line(line, VALDCO=-lv) for lv in (-3, -6, -10, -20)
                      for loop in loops[lv] for line in _open_lines(loop)]

    def at(kx, ky):
        c = kx * 1000 / GROUND / CELL + 1
        r = (Y1 - Y0 - ky * 1000 / GROUND) / CELL + 2
        ci, ri = int(c), int(r)
        if not (1 <= ci < NX and 2 <= ri < NY + 1):
            return None
        tx, ty = c - ci, r - ri
        a = f[ri][ci] * (1 - tx) + f[ri][ci + 1] * tx
        b = f[ri + 1][ci] * (1 - tx) + f[ri + 1][ci + 1] * tx
        return a * (1 - ty) + b * ty

    w = (X1 - X0) * GROUND / 1000
    h = (Y1 - Y0) * GROUND / 1000
    rnd = random.Random(42)
    soundings, rocks, houses = [], [], []
    for _ in range(30000):
        kx, ky = rnd.uniform(0, w), rnd.uniform(0, h)
        v = at(kx, ky)
        if v is None:
            continue
        if v < -0.3 and rnd.random() < 0.05:
            soundings.append(_km_point(kx, ky, DEPTH=round(-v, 1)))
        elif -3.0 < v < 0.6 and rnd.random() < 0.45:
            kind = 38513 if v > -0.2 else 38512 if v > -1.0 else 38511
            rocks.append(_km_point(kx, ky, kohdeluokka=kind))
        elif v > 1.5 and rnd.random() < (0.05 if ky > 12.8 or abs(kx - 6.4) < 1.6 else 0.015):
            houses.append(_km_point(kx, ky, kohdeluokka=42211))
    out["sounding"] = soundings
    out["rock"] = rocks
    out["building"] = [_poly([_house(x, y, rnd)], **feat["properties"])
                       for feat in houses for x, y in [feat["geometry"]["coordinates"]]]
    out["rockarea"] = [_poly([_km_line([(9.3, 6.9), (9.7, 7.1), (9.8, 6.7), (9.4, 6.5), (9.3, 6.9)])], kohdeluokka=38512)]
    # The Land Survey gives contour heights in millimetres.
    out["height"] = [_line(line, korkeusarvo=lv * 1000) for lv in range(5, 75, 5) for loop in _loops(f, lv)
                     for line in _open_lines([_to_merc(p) for p in loop])]
    out["road"] = [
        _line(_km_line([(0.0, 14.2), (3.0, 14.0), (5.5, 13.6), (6.6, 13.2), (9.5, 13.7), (13.4, 14.1)]), kohdeluokka=12121),
        _line(_km_line([(6.6, 13.2), (6.3, 11.4), (6.6, 9.6), (6.1, 7.6), (6.5, 5.6)]), kohdeluokka=12141),
        _line(_km_line([(6.3, 11.4), (5.1, 10.6), (4.8, 9.4)]), kohdeluokka=12313),
        _line(_km_line([(6.6, 9.6), (7.9, 9.3), (8.2, 8.2)]), kohdeluokka=12313),
        _line(_km_line([(6.1, 7.6), (6.9, 7.2)]), kohdeluokka=12313),
    ]
    out["lake.lake"] = [_poly([_km_line([(3.6, 14.6), (4.3, 14.8), (4.6, 14.4), (4.0, 14.1), (3.5, 14.3), (3.6, 14.6)])])]
    out["landmark.mast"] = [_km_point(8.4, 12.6, korkeus=131), _km_point(8.0, 12.2, korkeus=82),
                            _km_point(8.8, 12.3, korkeus=59)]
    out["fline"] = [_line(_km_line(BOAT_ROUTE), mitoitussyvays=2.4, jnro=4207)]
    out["route"] = [_line(_km_line(BOAT_ROUTE), vaylaluokkakoodi=4, jnro=4207)]
    out["lead"] = [_line(_km_line(LEAD), suunta=round(math.degrees(math.atan2(
        LEAD[1][0] - LEAD[2][0], LEAD[1][1] - LEAD[2][1])) % 360, 1))]
    out["cable.cable"] = [_line(_km_line(CABLE))]
    out["restrict"] = [_poly([_km_line([(6.95, 5.0), (7.85, 5.0), (7.85, 8.6), (6.95, 8.6), (6.95, 5.0)])],
                             rajoitustyypit="01", suuruus=5)]
    marks = [(4.6, 2.75, "Viitta", 1, "E", "Drages 1"), (4.6, 2.45, "Viitta", 2, "E", "Drages 2"),
             (6.6, 2.2, "Viitta", 2, "K", "Drages 3"), (7.25, 4.9, "Viitta", 1, "E", "Drages 4"),
             (7.55, 5.6, "Viitta", 2, "E", "Drages 5"), (7.2, 9.6, "Linjamerkki", 99, "K", "Dragesvik ylempi"),
             (7.35, 3.6, "Linjamerkki", 99, "K", "Dragesvik alempi"), (9.6, 3.9, "Sektoriloisto", 99, "K", "Rönnbåda"),
             (2.0, 2.3, "Viitta", 3, "E", "Sandö N"), (11.6, 3.0, "Poiju", 4, "K", "Svartö S"),
             (12.0, 4.25, "Suuntaloisto", 99, "K", "Svartö suunta")]
    out["aid"] = [_km_point(x, y, turvalaitetyyppifi=t, navigointilajikoodi=cat, valaistu=lit, nimifi=name)
                  for x, y, t, cat, lit, name in marks]
    out["light"] = [_km_point(9.6, 3.9, viralvalotunnuse="Fl WRG 3s"), _km_point(7.2, 9.6, viralvalotunnuse="Iso 4s"),
                    _km_point(7.35, 3.6, viralvalotunnuse="Iso 2s"), _km_point(6.6, 2.2, viralvalotunnuse="Fl G 3s"),
                    _km_point(11.6, 3.0, viralvalotunnuse="Q(6)+LFl 15s"),
                    _km_point(12.0, 4.25, viralvalotunnuse="Oc WRG 4s")]
    out["sector"] = [_km_point(9.6, 3.9, alkukulma=a, loppukulma=b, vari=col)
                     for a, b, col in ((300, 345, "vi"), (345, 352, "v"), (352, 40, "p"))] + [
        # A directional light's beam out to the south-west, between narrow green and red.
        _km_point(12.0, 4.25, alkukulma=a, loppukulma=b, vari=col)
        for a, b, col in ((39, 43, "vi"), (43, 47, "v"), (47, 51, "p"))] + [
        _km_point(x, y, alkukulma=0, loppukulma=360, vari=col)
        for x, y, col in ((7.2, 9.6, "v"), (7.35, 3.6, "v"), (6.6, 2.2, "vi"), (11.6, 3.0, "v"))]
    names = [(2.6, 8.4, "Sandöfjärden", "swe", 36000), (10.6, 6.2, "Svartöfjärden", "swe", 36000),
             (8.4, 7.4, "Källviklandet", "swe", 35020), (6.2, 5.6, "Porkkalanniemi", "fin", 35060),
             (6.2, 5.6, "Porkala udd", "swe", 35060), (7.7, 6.9, "Dragesviken", "swe", 36000),
             (3.1, 9.6, "Gråspajkan", "swe", 35070), (12.1, 4.6, "Svartö", "swe", 35070),
             (6.9, 13.4, "Dragesvik", "swe", 48120), (9.0, 1.4, "Rönnbådafjärden", "swe", 36000)]
    out["name"] = [_km_point(x, y, teksti=t, kieli=lang, kohdeluokka=code) for x, y, t, lang, code in names]
    return out


def box() -> tuple[float, float, float, float]:
    return X0, Y0, X1, Y1


def box_ring() -> list[list[float]]:
    return [[X0, Y0], [X1, Y0], [X1, Y1], [X0, Y1], [X0, Y0]]
