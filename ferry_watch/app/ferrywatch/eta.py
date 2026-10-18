"""Estimates when a ship will pass the point of its route nearest the phone.

Uses the track of an earlier passage in the same direction when one exists
(that follows the real fairway and its speed changes); otherwise straight-line
distance at the current speed.
"""

from __future__ import annotations

from typing import Any

from .geo import distance_m
from .store import Store

NEAR_ROUTE_M = 10_000  # phone must be this close to the earlier track
MAX_STRAIGHT_LINE_M = 30_000
KNOTS = 0.514444


def _nearest(track: list[list[float]], lat: float, lon: float) -> tuple[int, float]:
    best_i, best_d = 0, float("inf")
    for i, (plat, plon, _ts) in enumerate(track):
        d = distance_m(lat, lon, plat, plon)
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


def estimate(store: Store, passage: dict[str, Any], ship: tuple[float, float], sog: float | None,
             phone: tuple[float, float]) -> dict[str, Any] | None:
    """{"seconds": int, "method": "history" | "distance"}, or None if it can't tell
    (or the ship has already passed)."""
    earlier = [p for p in store.passages(limit=10, route=passage["route"], direction=passage["direction"],
                                         before_ts=passage["ts"]) if p["id"] != passage.get("id")]
    for prev in earlier:
        track = store.track(prev["mmsi"], prev["ts"] - 600, prev["ts"] + 3 * 3600)
        if len(track) < 5:
            continue
        i, d_ship = _nearest(track, *ship)
        j, d_phone = _nearest(track, *phone)
        if d_ship > NEAR_ROUTE_M or d_phone > NEAR_ROUTE_M:
            continue
        if j <= i:
            return None  # nearest point is behind the ship
        return {"seconds": int(track[j][2] - track[i][2]), "method": "history"}
    d = distance_m(*ship, *phone)
    if sog and sog > 3 and d <= MAX_STRAIGHT_LINE_M:
        return {"seconds": int(d / (sog * KNOTS)), "method": "distance"}
    return None
