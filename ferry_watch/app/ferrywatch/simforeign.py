"""Stand-ins for the drawn chart's foreign sources in the simulator: a made-up Estonian
north coast and Stockholm archipelago, as EMODnet's depth model (a GeoTIFF) and as
OpenStreetMap elements (the dicts foreign.OsmStream makes). See foreign.py."""

from __future__ import annotations

import math
import random
import re
import struct
import zlib
from typing import Any


def estonia_lat(lon: float) -> float:
    """The made-up Estonian shore (land to the south)."""
    return 59.47 + 0.035 * math.sin(lon * 6.0) + 0.012 * math.sin(lon * 21 + 1)


def sweden_lon(lat: float) -> float:
    """The made-up Swedish shore (land to the west)."""
    return 18.75 + 0.06 * math.sin(lat * 9) + 0.02 * math.sin(lat * 31)


def _islands() -> list[tuple[float, float, float]]:
    """(lon, lat, radius km)."""
    out = [(24.53, 59.575, 2.2), (24.76, 59.585, 0.8), (25.0, 59.62, 1.2), (25.42, 59.70, 0.25),
           (26.36, 59.82, 0.15), (23.4, 59.43, 0.9)]
    rnd = random.Random(7)
    for _ in range(70):
        lat = rnd.uniform(59.15, 60.3)
        lon = rnd.uniform(sweden_lon(lat) + 0.04, 19.3)
        out.append((round(lon, 4), round(lat, 4), round(rnd.choice((0.08, 0.15, 0.25, 0.4, 0.7, 1.1)), 2)))
    return out


ISLANDS = _islands()
REACH_KM = 15  # depths stop growing this far from the shore, so islands further off don't count
_BINS: dict[tuple[int, int], list] = {}
for _isl in ISLANDS:
    for _bx in range(int(_isl[0] / 0.05) - 7, int(_isl[0] / 0.05) + 8):
        for _by in range(int(_isl[1] / 0.05) - 4, int(_isl[1] / 0.05) + 5):
            _BINS.setdefault((_bx, _by), []).append(_isl)


def sea_km(lon: float, lat: float) -> float:
    """Distance to the nearest made-up shore in km, negative on land."""
    c = math.cos(math.radians(lat))
    d = min(REACH_KM, (lat - estonia_lat(lon)) * 111.2 if 21.5 <= lon <= 28.5 else 99,
            (lon - sweden_lon(lat)) * 111.2 * c if 58.6 <= lat <= 61.5 else 99)
    for ilon, ilat, r in _BINS.get((int(lon / 0.05), int(lat / 0.05)), ()):
        d = min(d, math.hypot((lon - ilon) * 111.2 * c, (lat - ilat) * 111.2) - r)
    return d


def depth_at(lon: float, lat: float) -> float:
    d = sea_km(lon, lat)
    if d <= 0:
        return -3.0
    return min(160.0, 1.0 + 9 * d ** 0.9 + 3 * math.sin(lon * 40) * math.sin(lat * 60))


def lzw_encode(data: bytes) -> bytes:
    """TIFF's LZW (for tests of the decoder)."""
    table = {bytes([i]): i for i in range(256)}
    out, acc, nbits, width = bytearray(), 0, 0, 9

    def put(code: int) -> None:
        nonlocal acc, nbits
        acc = (acc << width) | code
        nbits += width
        while nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)

    put(256)
    cur = b""
    nxt = 258
    for byte in data:
        cand = cur + bytes([byte])
        if cand in table:
            cur = cand
            continue
        put(table[cur])
        table[cand] = nxt
        nxt += 1
        if nxt >= (1 << width) and width < 12:
            width += 1
        cur = bytes([byte])
    if cur:
        put(table[cur])
        if nxt + 1 >= (1 << width) and width < 12:
            width += 1
    put(257)
    if nbits:
        out.append((acc << (8 - nbits)) & 0xFF)
    return bytes(out)


def geotiff(w: int, h: int, lon0: float, lat0: float, dlon: float, dlat: float, values: list[float],
            nodata: float = -99999.0, compression: int = 1, predictor: int = 1) -> bytes:
    """A minimal little-endian GeoTIFF: one strip of float32, degrees; compression 1 (none),
    5 (LZW) or 8 (Deflate), predictor 1 (none) or 3 (floating point)."""
    data = struct.pack(f"<{w * h}f", *values)
    if predictor == 3:
        out = bytearray()
        for r in range(h):
            row = struct.pack(f">{w}f", *values[r * w:(r + 1) * w])
            planes = bytes(row[c * 4 + b] for b in range(4) for c in range(w))
            out += bytes([planes[0]] + [(planes[k] - planes[k - 1]) & 0xFF for k in range(1, len(planes))])
        data = bytes(out)
    if compression == 8:
        data = zlib.compress(data)
    elif compression == 5:
        data = lzw_encode(data)
    nodata_s = f"{nodata:g}".encode() + b"\x00"
    entries = []
    extra = bytearray()
    base = 8  # the IFD follows the header; extra values after the IFD
    tags = [(256, 4, [w]), (257, 4, [h]), (258, 3, [32]), (259, 3, [compression]), (262, 3, [1]), (273, 4, [0]),
            (277, 3, [1]), (278, 4, [h]), (279, 4, [len(data)]), (317, 3, [predictor]), (339, 3, [3]),
            (33550, 12, [dlon, dlat, 0.0]), (33922, 12, [0.0, 0.0, 0.0, lon0, lat0, 0.0]), (42113, 2, nodata_s)]
    ifd_size = 2 + 12 * len(tags) + 4
    fmt = {3: "H", 4: "I", 12: "d"}
    for tag, typ, vals in tags:
        if typ == 2:
            raw, count = bytes(vals), len(vals)
        else:
            raw, count = struct.pack("<" + fmt[typ] * len(vals), *vals), len(vals)
        if len(raw) <= 4:
            entries.append((tag, typ, count, raw.ljust(4, b"\x00"), None))
        else:
            entries.append((tag, typ, count, None, len(extra)))
            extra += raw
            if len(extra) % 2:
                extra += b"\x00"
    data_off = base + ifd_size + len(extra)
    out = bytearray(b"II*\x00" + struct.pack("<I", base))
    out += struct.pack("<H", len(entries))
    for tag, typ, count, inline, off in entries:
        if tag == 273:
            inline = struct.pack("<I", data_off)
        out += struct.pack("<HHI", tag, typ, count)
        out += inline if inline is not None else struct.pack("<I", base + ifd_size + off)
    out += struct.pack("<I", 0)
    out += extra
    out += data
    return bytes(out)


async def fake_dtm(lon0: float, lat0: float, lon1: float, lat1: float) -> bytes:
    """EMODnet's GetCoverage for a box: 1/16 arc minute cells, elevation (negative under water)."""
    step = 1 / 960
    x0, y1 = math.floor(lon0 / step), math.ceil(lat1 / step)
    w = max(1, math.ceil(lon1 / step) - x0)
    h = max(1, y1 - math.floor(lat0 / step))
    values = []
    for j in range(h):
        lat = (y1 - j - 0.5) * step
        for i in range(w):
            lon = (x0 + i + 0.5) * step
            d = depth_at(lon, lat)
            values.append(-99999.0 if d < -2 and (i + j) % 3 == 0 else -d)  # some land has no data
    return geotiff(w, h, x0 * step, y1 * step, step, step, values)


def _ellipse(lon: float, lat: float, r_km: float, n: int = 40) -> list[tuple[float, float]]:
    c = math.cos(math.radians(lat))
    pts = []
    for k in range(n):
        a = 2 * math.pi * k / n  # counter-clockwise: land on the left
        f = 1 + 0.18 * math.sin(3 * a + lon * 50)
        pts.append((lon + r_km * f * math.cos(a) / (111.2 * c), lat + r_km * f * math.sin(a) / 111.2))
    return pts + pts[:1]


def _coast(s: float, w: float, n: float, e: float) -> list[dict]:
    out = []
    if s <= 59.6 and n >= 59.3 and e >= 21.5 and w <= 28.5:
        # Westward, so the land (south) is on the left.
        pts = [(lon, estonia_lat(lon)) for lon in (28.5 - k * 0.002 for k in range(3501))]
        out.append({"type": "way", "tags": {}, "geometry": pts})
    if e >= 18.5 and w <= 19.0 and s <= 61.5 and n >= 58.6:
        pts = [(sweden_lon(lat), lat) for lat in (58.6 + k * 0.001 for k in range(2901))]
        out.append({"type": "way", "tags": {}, "geometry": pts})
    for lon, lat, r in ISLANDS:
        if w - 0.05 <= lon <= e + 0.05 and s - 0.03 <= lat <= n + 0.03:
            out.append({"type": "way", "tags": {}, "geometry": _ellipse(lon, lat, r, 16 + int(r * 40))})
    return out


def _node(lon: float, lat: float, **tags: Any) -> dict:
    return {"type": "node", "lat": lat, "lon": lon, "tags": {k.replace("__", ":"): str(v) for k, v in tags.items()}}


def _way(points: list, **tags: Any) -> dict:
    return {"type": "way", "geometry": points, "tags": {k.replace("__", ":"): str(v) for k, v in tags.items()}}


def _marks() -> list[dict]:
    lat_ = "seamark__light__"
    out = [
        # Estonia: lighthouses, the Tallinn approach and cardinals off Naissaar.
        _node(24.51, 59.598, **{"seamark__type": "light_major", "seamark__name": "Naissaar", lat_ + "character": "Fl",
                                lat_ + "group": "2", lat_ + "period": "12", lat_ + "colour": "white",
                                lat_ + "range": "12"}),
        _node(25.42, 59.70, **{"seamark__type": "light_major", "seamark__name": "Keri", lat_ + "character": "LFl",
                               lat_ + "period": "10", lat_ + "colour": "white", lat_ + "range": "13"}),
        _node(24.69, 59.495, **{"seamark__type": "light_minor", "seamark__name": "Tallinn sektor",
                                lat_ + "1__colour": "green", lat_ + "1__sector_start": "140", lat_ + "1__sector_end": "160",
                                lat_ + "1__character": "Iso", lat_ + "1__period": "4",
                                lat_ + "2__colour": "white", lat_ + "2__sector_start": "160", lat_ + "2__sector_end": "166",
                                lat_ + "2__character": "Iso", lat_ + "2__period": "4",
                                lat_ + "3__colour": "red", lat_ + "3__sector_start": "166", lat_ + "3__sector_end": "190",
                                lat_ + "3__character": "Iso", lat_ + "3__period": "4"}),
        _node(24.52, 59.64, **{"seamark__type": "buoy_cardinal", "seamark__buoy_cardinal__category": "north",
                               "seamark__buoy_cardinal__shape": "pillar", "seamark__name": "Naissaare N",
                               lat_ + "character": "Q", lat_ + "colour": "white"}),
        _node(24.60, 59.555, **{"seamark__type": "buoy_cardinal", "seamark__buoy_cardinal__category": "east",
                                "seamark__buoy_cardinal__shape": "spar"}),
        _node(24.86, 59.66, **{"seamark__type": "buoy_safe_water", "seamark__buoy_safe_water__shape": "pillar",
                               lat_ + "character": "LFl", lat_ + "period": "10", lat_ + "colour": "white"}),
        _node(24.62, 59.47, **{"seamark__type": "beacon_lateral", "seamark__beacon_lateral__category": "port"}),
        _node(24.60, 59.62, **{"seamark__type": "pilot_boarding", "seamark__name": "Tallinn"}),
        _node(24.95, 59.56, **{"seamark__type": "wreck", "seamark__wreck__category": "dangerous"}),
        _node(24.67, 59.56, **{"seamark__type": "rock", "seamark__rock__water_level": "awash"}),
        _way([(24.69, 59.495), (24.80, 59.66)], **{"seamark__type": "navigation_line",
                                                    "seamark__navigation_line__orientation": "333"}),
        # The Estonian half of a traffic separation scheme.
        _way([(25.6, 59.69), (24.6, 59.69)], **{"seamark__type": "separation_lane"}),  # westbound
        _way([(24.6, 59.65), (25.6, 59.65)], **{"seamark__type": "separation_lane"}),  # eastbound
        _way([(24.6, 59.665), (25.6, 59.665), (25.6, 59.675), (24.6, 59.675), (24.6, 59.665)],
             **{"seamark__type": "separation_zone"}),
        _way([(24.6, 59.71), (25.6, 59.71)], **{"seamark__type": "separation_boundary"}),
        _way([(24.6, 59.63), (25.6, 59.63)], **{"seamark__type": "separation_boundary"}),
        # Sweden: Söderarm, Almagrundet and the Sandhamn approach.
        _node(19.41, 59.755, **{"seamark__type": "light_major", "seamark__name": "Söderarm", lat_ + "character": "Fl",
                                lat_ + "group": "3", lat_ + "period": "15", lat_ + "colour": "white",
                                lat_ + "range": "15"}),
        _node(19.13, 59.155, **{"seamark__type": "light_major", "seamark__name": "Almagrundet",
                                lat_ + "character": "Mo(U)", lat_ + "period": "15", lat_ + "colour": "white",
                                lat_ + "range": "14"}),
    ]
    for k in range(8):
        lat = 59.27 + 0.006 * k
        out.append(_node(18.98 + 0.01 * k, lat, **{"seamark__type": "buoy_lateral",
                                                   "seamark__buoy_lateral__category": "port" if k % 2 else "starboard",
                                                   "seamark__buoy_lateral__shape": "spar"}))
    return out


NAMES = [(24.75, 59.437, "Tallinn", "city"), (24.53, 59.575, "Naissaar", "island"), (25.5, 59.76, "Soome laht", "sea"),
         (24.05, 59.35, "Paldiski", "town"), (18.07, 59.33, "Stockholm", "city"), (18.92, 59.28, "Sandhamn", "village"),
         (19.2, 60.05, "Ålands hav", "sea"), (24.74, 59.50, "Tallinna laht", "bay")]


async def fake_osm(query: str) -> list[dict]:
    """Overpass, for the two queries foreign.py makes."""
    boxes = [tuple(float(v) for v in m) for m in re.findall(r"\(([\d.]+),([\d.]+),([\d.]+),([\d.]+)\)", query)]
    if not boxes:
        return []
    s, w, n, e = boxes[0]
    if "coastline" in query:
        return _coast(s, w, n, e)

    def inside(lon: float, lat: float) -> bool:
        return s <= lat <= n and w <= lon <= e

    def crosses(points: list) -> bool:
        return (min(p[1] for p in points) <= n and max(p[1] for p in points) >= s
                and min(p[0] for p in points) <= e and max(p[0] for p in points) >= w)

    out = [el for el in _marks() if (el["type"] == "node" and inside(el["lon"], el["lat"]))
           or (el["type"] == "way" and crosses(el["geometry"]))]
    for lon, lat, name, kind in NAMES:
        if inside(lon, lat):
            key = "natural" if kind == "bay" else "place"
            out.append({"type": "node", "lat": lat, "lon": lon, "tags": {key: kind, "name": name}})
    return out
