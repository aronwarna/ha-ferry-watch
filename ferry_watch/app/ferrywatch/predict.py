"""Next passage: when a ship under way will reach the Porkkala lines, and how often it has
taken the inner route lately.

The time comes from the tracks of earlier passages, which follow the real fairway and its
speed changes: find the point of an earlier track nearest the ship, heading the same way,
and take how long that ship needed from there to the line. Cheap SQL on data already
stored; at most once a minute per ship.
"""

from __future__ import annotations

import math
import statistics
import time
from typing import Any, Callable

from .geo import angle_diff, distance_m
from .store import Store

MIN_SPEED = 5.0  # knots; slower ships (in port, manoeuvring) get no prediction
MATCH_M = 2000  # the ship must be this close to an earlier passage's track
MATCH_DEGREES = 45  # ...and heading the same way along it
LOOKBACK = 3 * 3600  # how much of the track before each passage is searched
PASSAGES = 12  # earlier passages tried, the ship's own first
MATCHES = 3  # the median of this many matching passages is used
SPEED_RATIO = (0.7, 1.4)  # limits of the correction for sailing faster or slower than before
HABIT_DAYS = 28
HOME_PORT = "Helsinki"  # each sailing leaves from (or returns to) Helsinki
RECOMPUTE = 60  # seconds between predictions for one ship
PAST_GRACE = 120  # a prediction this far in the past is dropped
STALE = 5 * 60  # positions older than this don't get a prediction


def bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    x = math.sin(dl) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _match(track: list[list[float]], lat: float, lon: float, course: float) -> int | None:
    """Index of the track point nearest (lat, lon) if it is close and the track runs the
    same way there; else None."""
    best_i, best_d = -1, float("inf")
    for i, p in enumerate(track):
        d = distance_m(lat, lon, p[0], p[1])
        if d < best_d:
            best_i, best_d = i, d
    if best_d > MATCH_M:
        return None
    j = max(1, min(best_i + 1, len(track) - 1))
    a, b = track[j - 1], track[j]
    if (a[0], a[1]) == (b[0], b[1]) or angle_diff(bearing((a[0], a[1]), (b[0], b[1])), course) > MATCH_DEGREES:
        return None
    return best_i


def inner_habit(store: Store, mmsi: int, direction: str, now: float) -> dict[str, int]:
    """{"inner": n, "of": m}: inner-route passages in this direction in the last HABIT_DAYS,
    out of the sailings in that time (departures from Helsinki, which match the arrivals)."""
    since = int(now - HABIT_DAYS * 86400)
    inner = store.count_passages(mmsi, "inner", direction, since)
    of = max(store.voyages_from(mmsi, HOME_PORT, since), inner)
    return {"inner": inner, "of": of} if of else {}


def predict(store: Store, mmsi: int, pos: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """{"eta_ts", "direction", "inner", "of"} for a ship at `pos` (a position dict), or None."""
    now = now or time.time()
    sog = pos.get("sog")
    course = pos.get("cog") if pos.get("cog") is not None else pos.get("heading")
    if sog is None or sog < MIN_SPEED or course is None:
        return None
    etas: list[float] = []
    direction = None
    for passage in store.recent_passages_for(mmsi, PASSAGES):
        track = store.track(passage["mmsi"], passage["ts"] - LOOKBACK, passage["ts"], speed=True)
        if len(track) < 3:
            continue
        i = _match(track, pos["lat"], pos["lon"], course)
        if i is None:
            continue
        seconds = passage["ts"] - track[i][2]
        if seconds <= 0:
            continue
        before = track[i][3]
        if before:  # the earlier ship sailed faster or slower here
            seconds *= min(SPEED_RATIO[1], max(SPEED_RATIO[0], before / sog))
        etas.append(seconds)
        direction = direction or passage["direction"]
        if len(etas) >= MATCHES:
            break
    if not etas:
        return None
    return {"eta_ts": int(pos["ts"] + statistics.median(etas)), "direction": direction,
            **inner_habit(store, mmsi, direction, now)}


class Predictor:
    """Keeps the latest prediction for each ship of the main tracker, from its position
    events, and calls on_change when one changed."""

    def __init__(self, store: Store, on_change: Callable[[], None] | None = None):
        self.store = store
        self.on_change = on_change
        self.predictions: dict[int, dict[str, Any]] = {}
        self._computed: dict[int, float] = {}

    def on_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind != "position":
            return
        mmsi, pos = payload["mmsi"], payload.get("position")
        now = time.time()
        if now - self._computed.get(mmsi, 0.0) < RECOMPUTE or (pos and now - pos["ts"] > STALE):
            return  # too soon, or an old position (catching up after a restart)
        self._computed[mmsi] = now
        result = None if payload.get("in_port") or not pos else predict(self.store, mmsi, pos, now)
        before = self.predictions.get(mmsi)
        if result:
            self.predictions[mmsi] = result
        else:
            self.predictions.pop(mmsi, None)
        if result != before and self.on_change:
            self.on_change()

    def get(self, mmsi: int, now: float | None = None) -> dict[str, Any] | None:
        p = self.predictions.get(mmsi)
        if p and p["eta_ts"] < (now or time.time()) - PAST_GRACE:
            return None
        return p

    def soonest(self, mmsis: set[int], now: float | None = None) -> tuple[int, dict[str, Any]] | None:
        found = [(p["eta_ts"], m, p) for m in mmsis if (p := self.get(m, now))]
        if not found:
            return None
        _, mmsi, p = min(found)
        return mmsi, p
