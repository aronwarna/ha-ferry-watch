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
