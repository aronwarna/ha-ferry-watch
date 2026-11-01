"""Builds the add-on's base map (ferrywatch/ne_base.json) and the blocks "Keep chart on NAS"
downloads (ferrywatch/keep_blocks.json). Not part of the add-on: run by hand with shapely.

    python3 tools/build_ne_base.py <natural-earth-dir>

<natural-earth-dir> holds Natural Earth's 1:10m GeoJSON (public domain, naturalearthdata.com):
ne_10m_land, ne_10m_minor_islands, ne_10m_lakes and ne_10m_lakes_europe (.geojson).

ne_base.json: {"land": [...], "lakes": [...], "fi_sea": [...]},
each a list of polygons [outer, hole, ...] with rings as flat [lon, lat, ...] (3 decimals),
outer rings anticlockwise and holes clockwise. "land" and "lakes" cover 5-40°E 53-75°N; fi_sea
is FINNISH_WATERS less the land. Land and sea
come cut on a 1° by 0.5° grid (lakes are outlined, so they stay whole).

keep_blocks.json: [[bx, by], ...] zoom-11 blocks with Finnish water (sea, or a lake) in them,
the Gulf of Finland first, then the Archipelago Sea and Åland, the Bothnian Sea, the Bay of
Bothnia and last the lakes.
"""
import ast
import json
import math
import sys
from pathlib import Path

from shapely.geometry import Polygon, box, shape
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from shapely.prepared import prep

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "ferry_watch" / "app" / "ferrywatch"
# FINNISH_WATERS as charts.py has it (read from its source: charts.py needs the add-on's packages).
FINNISH_WATERS = next(ast.literal_eval(node.value) for node in ast.parse((APP / "charts.py").read_text()).body
                      if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "FINNISH_WATERS")

BOX = box(5, 53, 40, 75)
TOL = 0.002  # degrees (about 100-200 m): plenty for a base drawn zoomed out
MIN_EXTENT = 0.004
BLOCK_Z = 11


def load(ne: Path, name: str):
    geoms = []
    for f in json.loads((ne / f"{name}.geojson").read_text())["features"]:
        g = shape(f["geometry"])
        if not g.is_valid:
            g = g.buffer(0)
        if g.intersects(BOX):
            geoms.append(g.intersection(BOX))
    return unary_union(geoms)


def polys(g):
    if isinstance(g, Polygon):
        return [g] if not g.is_empty else []
    return [p for part in getattr(g, "geoms", []) for p in polys(part)]


def flat(ring) -> list:
    out = []
    for x, y in list(ring.coords)[:-1]:
        out += [round(x, 3), round(y, 3)]
    return out


def split(g, dx: float = 1.0, dy: float = 0.5):
    """Big polygons cut on a grid (dx by dy degrees), so a tile clips only the pieces near it.
    Only for what's filled as one shape, never outlined (the cuts would show)."""
    pieces = []
    for p in polys(g):
        px0, py0, px1, py1 = p.bounds
        if px1 - px0 <= dx and py1 - py0 <= dy:
            pieces.append(p)
            continue
        i = math.floor(px0 / dx)
        while i * dx < px1:
            j = math.floor(py0 / dy)
            while j * dy < py1:
                pieces += polys(p.intersection(box(i * dx, j * dy, (i + 1) * dx, (j + 1) * dy)))
                j += 1
            i += 1
    return pieces


def export(g, cut: bool = False) -> list:
    out = []
    g = g.simplify(TOL, preserve_topology=True)
    for p in split(g) if cut else polys(g):
        p = orient(p, 1.0)  # outer rings anticlockwise, holes clockwise: filled nonzero, holes stay

        x0, y0, x1, y1 = p.bounds
        if x1 - x0 < MIN_EXTENT and y1 - y0 < MIN_EXTENT:
            continue
        rings = [flat(p.exterior)]
        for h in p.interiors:
            hx0, hy0, hx1, hy1 = h.bounds
            if max(hx1 - hx0, hy1 - hy0) >= MIN_EXTENT:
                rings.append(flat(h))
        rings = [r for r in rings if len(r) >= 6]
        if rings and len(rings[0]) >= 6:
            out.append(rings)
    return out


def tile_box(x: int, y: int, z: int = BLOCK_Z):
    def lat(t):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * t / 2 ** z))))
    return box(x / 2 ** z * 360 - 180, lat(y + 1), (x + 1) / 2 ** z * 360 - 180, lat(y))


def tile_of(lon: float, lat: float, z: int = BLOCK_Z) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def main() -> None:
    ne = Path(sys.argv[1])
    fw = Polygon(FINNISH_WATERS)
    land = unary_union([load(ne, "ne_10m_land"), load(ne, "ne_10m_minor_islands")])
    lakes = unary_union([load(ne, "ne_10m_lakes"), load(ne, "ne_10m_lakes_europe")])
    sea = fw.difference(land)
    data = {
        "land": export(land, True),
        "lakes": export(lakes),
        "fi_sea": export(sea, True),
    }
    out = APP / "ne_base.json"
    out.write_text(json.dumps(data, separators=(",", ":")))
    for k, v in data.items():
        print(k, len(v), "polygons", sum(len(r) // 2 for p in v for r in p), "vertices")
    print(out, out.stat().st_size, "bytes")

    # Blocks to keep: all of the Gulf of Finland as kept before (KEEP_AREA, within about 30 km
    # of FINNISH_WATERS), then the sea a little past Natural Earth's shore (its bays and
    # inner archipelago reach further in than that coarse shore), and the Finnish lakes.
    gulf_x0, gulf_y1 = tile_of(22.7, 59.45)
    gulf_x1, gulf_y0 = tile_of(28.2, 60.75)
    fw_near = prep(fw.buffer(0.5))
    sea_near = prep(sea.buffer(0.03))
    lakes_fi = prep(lakes.intersection(fw).buffer(0.005))
    x0, y0 = tile_of(19.0, 70.2)
    x1, y1 = tile_of(31.7, 59.2)
    blocks = []
    for bx in range(min(x0, gulf_x0), max(x1, gulf_x1) + 1):
        for by in range(min(y0, gulf_y0), max(y1, gulf_y1) + 1):
            b = tile_box(bx, by)
            c = b.centroid
            if gulf_x0 <= bx <= gulf_x1 and gulf_y0 <= by <= gulf_y1:
                if fw_near.intersects(b):
                    blocks.append((0, by, bx))
                continue
            if not b.intersects(fw):
                continue
            if sea_near.intersects(b):
                rank = 1 if c.y < 60.95 else 2 if c.y < 63.5 else 3
            elif lakes_fi.intersects(b):
                rank = 4
            else:
                continue
            blocks.append((rank, by, bx))
    blocks.sort()
    keep = APP / "keep_blocks.json"
    keep.write_text(json.dumps([[bx, by] for _, by, bx in blocks], separators=(",", ":")))
    print(keep, len(blocks), "blocks:", {r: sum(b[0] == r for b in blocks) for r in range(5)})


if __name__ == "__main__":
    main()
