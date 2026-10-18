"""Small geometry helpers."""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000

# Ports the tracked ferries call at, used to label voyages ("since Helsinki").
PORTS = [
    ("Helsinki", 60.160, 24.955),
    ("Stockholm", 59.350, 18.100),
    ("Mariehamn", 60.090, 19.930),
    ("Långnäs", 60.117, 20.296),
    ("Tallinn", 59.445, 24.770),
    ("Turku", 60.435, 22.220),
    ("Kapellskär", 59.720, 19.065),
]
PORT_RADIUS_M = 8_000


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def nearest_port(lat: float, lon: float) -> str | None:
    best, best_d = None, PORT_RADIUS_M
    for name, plat, plon in PORTS:
        d = distance_m(lat, lon, plat, plon)
        if d <= best_d:
            best, best_d = name, d
    return best


def angle_diff(a: float, b: float) -> float:
    """Smallest absolute difference between two bearings in degrees."""
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def _xy(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    """Local planar coordinates (degrees, longitude scaled), fine over tens of km."""
    return lon * math.cos(math.radians(lat0)), lat


def _orient(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(p1: tuple[float, float], p2: tuple[float, float],
                   q1: tuple[float, float], q2: tuple[float, float]) -> bool:
    """True if segment p1-p2 crosses segment q1-q2. Points are (lat, lon)."""
    lat0 = (p1[0] + p2[0] + q1[0] + q2[0]) / 4
    a, b, c, d = (_xy(*pt, lat0) for pt in (p1, p2, q1, q2))
    d1, d2 = _orient(c, d, a), _orient(c, d, b)
    d3, d4 = _orient(a, b, c), _orient(a, b, d)
    # A track point lying exactly on the gate counts once: on the side it ends up.
    return ((d1 > 0 > d2) or (d1 < 0 < d2) or (d2 == 0 and d1 != 0)) and \
        ((d3 > 0 > d4) or (d3 < 0 < d4) or d3 == 0 or d4 == 0)


def line_crossed(p1: tuple[float, float], p2: tuple[float, float], line: list[list[float]]) -> bool:
    """True if the move p1 -> p2 crosses any segment of a polyline [[lat, lon], ...]."""
    return any(segments_cross(p1, p2, tuple(a), tuple(b)) for a, b in zip(line, line[1:]))


def point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    inside = False
    n = len(polygon)
    for i in range(n):
        y1, x1 = polygon[i]
        y2, x2 = polygon[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_cross:
                inside = not inside
    return inside
