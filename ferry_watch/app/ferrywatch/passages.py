"""Detects when a ship passes through a gate line or an area.

Gate lines: a passage is counted when the straight move between two
consecutive positions crosses the line, so a ship can't slip past between
reports. Areas: a passage is counted after AREA_MIN_POINTS consecutive
positions inside the area. Either way the ship must be moving at least
MIN_SPEED knots, and each ship gets at most one passage per route per
COOLDOWN.

Sailings that take the outer route cross no inner line, so without outer-route lines
they would leave no trace and the stats couldn't compare the routes (by wind, say). A
ship crossing the Porkkala meridian with no inner passage in the last COOLDOWN is
therefore recorded as an outer passage (zone 0), unless outer-route lines exist. The
meridian lies between the two ends of the inner route, so on the inner route the line
has already been crossed; should an inner passage still follow (one line only), it
replaces the outer one.

Every passage is also timed at one fixed place (porkkala_ts, to the second between two
reports), so the stats compare like with like; a passage's own time is where it first
crossed a route line, which can be quarter of an hour away. Inner passages are timed
where the ship goes between Utterböte and Östra Styrskär off the Porkkala cape
(INNER_TIMING_GATE), outer ones where it crosses the meridian. An inner sailing that
misses the gate is timed at the meridian instead.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .geo import crossing_fraction, line_crossed, point_in_polygon
from .store import Position, Store

_LOGGER = logging.getLogger(__name__)

MIN_SPEED = 5.0  # knots
MAX_GAP = 20 * 60  # ignore moves between positions further apart than this
COOLDOWN = 2 * 3600
AREA_MIN_POINTS = 2
ROUTES = ("inner", "outer")
PORKKALA_MERIDIAN = 24.40
MERIDIAN_LATS = (59.55, 60.15)  # from the outer route to the coast
AUTO_OUTER = {"id": 0, "role": "outer"}
# Inner passages are timed on this line between two islets off the Porkkala cape, where
# the inner fairway runs between them: Utterböte to Östra Styrskär (islet centres,
# GeoNames / National Land Survey names). Also drawn by app.js (INNER_GATE).
INNER_TIMING_GATE = ((59.96566, 24.45308), (59.95250, 24.45639))

DIRECTION_LABELS = {"east": "towards Helsinki", "west": "towards Stockholm"}

PassageHandler = Callable[[dict[str, Any]], None]


def direction(prev: Position, pos: Position) -> str:
    if pos.lon != prev.lon:
        return "east" if pos.lon > prev.lon else "west"
    cog = pos.cog if pos.cog is not None else prev.cog
    return "east" if cog is not None and 0 < cog < 180 else "west"


def meridian_time(prev: Any, pos: Any) -> int | None:
    """When the move crossed (or ended on) the Porkkala meridian between the outer route
    and the coast, assuming a steady move between the two reports; None if it didn't."""
    m = PORKKALA_MERIDIAN
    if not ((prev.lon - m) * (pos.lon - m) < 0 or (pos.lon == m and prev.lon != m)):
        return None
    f = (m - prev.lon) / (pos.lon - prev.lon)
    lat = prev.lat + (pos.lat - prev.lat) * f
    if not MERIDIAN_LATS[0] <= lat <= MERIDIAN_LATS[1]:
        return None
    return round(prev.ts + (pos.ts - prev.ts) * f)


def gate_time(prev: Any, pos: Any) -> int | None:
    """When the move went through the inner timing gate (Utterböte to Östra Styrskär),
    assuming a steady move between the two reports; None if it didn't."""
    f = crossing_fraction((prev.lat, prev.lon), (pos.lat, pos.lon), *INNER_TIMING_GATE)
    return None if f is None else round(prev.ts + (pos.ts - prev.ts) * f)


def timing_point(route: str, crossings: dict[str, int], ts: int) -> int | None:
    """A passage's time at its timing place, from the ship's latest crossings of the gate
    and the meridian: the gate for an inner passage (the meridian if it missed the gate),
    the meridian for an outer one. Crossings more than COOLDOWN away belong to other trips."""
    for place in ("gate", "meridian") if route == "inner" else ("meridian",):
        at = crossings.get(place)
        if at is not None and abs(ts - at) < COOLDOWN:
            return at
    return None


def crosses_meridian(prev: Position, pos: Position) -> bool:
    """The move crosses (or ends on) the Porkkala meridian between the outer route and the coast."""
    return meridian_time(prev, pos) is not None


class Detector:
    def __init__(self, store: Store, on_passage: PassageHandler | None = None):
        self.store = store
        self.on_passage = on_passage
        self.prev: dict[int, Position] = {}
        self.area_run: dict[tuple[int, int], list[Position]] = {}
        self.crossed: dict[int, dict[str, int]] = {}  # mmsi -> {"gate"/"meridian": when}
        self.zones: list[dict[str, Any]] = []
        self.reload_zones()

    def reload_zones(self) -> None:
        self.zones = [z for z in self.store.zones() if z["role"] in ROUTES]
        roles = {z["role"] for z in self.zones}
        self.auto_outer = "inner" in roles and "outer" not in roles
        self.area_run.clear()

    def on_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind != "position" or not payload.get("position"):
            return
        p = payload["position"]
        self.on_position(Position(mmsi=payload["mmsi"], ts=p["ts"], lat=p["lat"], lon=p["lon"],
                                  sog=p["sog"], cog=p["cog"], heading=p["heading"]))

    def on_position(self, pos: Position) -> list[dict[str, Any]]:
        prev = self.prev.get(pos.mmsi)
        self.prev[pos.mmsi] = pos
        found = []
        moving = pos.sog is not None and pos.sog >= MIN_SPEED
        steady = bool(prev) and moving and pos.ts - prev.ts <= MAX_GAP
        # The timing places first, so a passage recorded on this same move is timed too.
        gate_at = gate_time(prev, pos) if steady else None
        at = meridian_time(prev, pos) if steady else None
        crossed = self.crossed.setdefault(pos.mmsi, {})
        if gate_at is not None:
            crossed["gate"] = gate_at
        if at is not None:
            crossed["meridian"] = at
        for zone in self.zones:
            if zone["shape"] == "line":
                if (prev and moving and pos.ts - prev.ts <= MAX_GAP
                        and line_crossed((prev.lat, prev.lon), (pos.lat, pos.lon), zone["points"])):
                    found.append(self._record(zone, prev, pos))
            else:
                key = (pos.mmsi, zone["id"])
                if moving and point_in_polygon(pos.lat, pos.lon, zone["points"]):
                    run = self.area_run.setdefault(key, [])
                    if run and pos.ts - run[-1].ts > MAX_GAP:
                        run.clear()
                    run.append(pos)
                    if len(run) == AREA_MIN_POINTS:
                        found.append(self._record(zone, run[0], run[1]))
                else:
                    self.area_run.pop(key, None)
        if at is not None and self.auto_outer and not any(found):
            inner = self.store.last_passage(pos.mmsi, "inner")
            if not inner or pos.ts - inner["ts"] >= COOLDOWN:
                found.append(self._record(AUTO_OUTER, prev, pos))
        # A passage recorded earlier (at its first line, before the timing place) is
        # timed now: an inner one at the gate, even over a meridian stand-in time.
        if gate_at is not None:
            inner = self.store.last_passage(pos.mmsi, "inner")
            if inner and abs(gate_at - inner["ts"]) < COOLDOWN and inner.get("porkkala_ts") != gate_at:
                self.store.set_porkkala_ts(inner["id"], gate_at)
        if at is not None:
            recent = self.store.passage_without_porkkala(pos.mmsi, pos.ts - COOLDOWN)
            if recent:
                self.store.set_porkkala_ts(recent["id"], at)
        return [p for p in found if p]

    def _record(self, zone: dict[str, Any], prev: Position, pos: Position) -> dict[str, Any] | None:
        last = self.store.last_passage(pos.mmsi, zone["role"])
        if last and pos.ts - last["ts"] < COOLDOWN:
            return None
        passage = {
            "mmsi": pos.mmsi,
            "ts": pos.ts,
            "route": zone["role"],
            "direction": direction(prev, pos),
            "zone_id": zone["id"],
            "lat": pos.lat,
            "lon": pos.lon,
            "sog": pos.sog,
        }
        if passage["route"] == "inner":
            self.store.delete_auto_outer(pos.mmsi, pos.ts - COOLDOWN)  # one inner line only
        porkkala_ts = timing_point(passage["route"], self.crossed.get(pos.mmsi, {}), pos.ts)
        passage["id"] = self.store.add_passage(passage)
        if porkkala_ts is not None:
            self.store.set_porkkala_ts(passage["id"], porkkala_ts)
            passage["porkkala_ts"] = porkkala_ts
        _LOGGER.info("%s: %s route passage %s", pos.mmsi, passage["route"], passage["direction"])
        if self.on_passage:
            self.on_passage(passage)
        return passage


def backfill_timing(store: Store, since_ts: int) -> int:
    """Time the passages recorded before their timing place was known, from the tracks
    still stored: inner ones at the gate (or the meridian), outer ones at the meridian.
    Inner passages are re-timed even if they have a time (it was the meridian's before
    the gate existed). Returns how many got a time."""
    done = 0
    for p in store.passages_to_time(since_ts):
        track = store.track(p["mmsi"], p["ts"] - COOLDOWN, p["ts"] + COOLDOWN, speed=True)
        best: dict[str, int] = {}
        for a, b in zip(track, track[1:]):
            if b[2] - a[2] > MAX_GAP or b[3] is None or b[3] < MIN_SPEED:
                continue
            prev = Position(p["mmsi"], a[2], a[0], a[1], a[3], None, None)
            pos = Position(p["mmsi"], b[2], b[0], b[1], b[3], None, None)
            for place, at in (("gate", gate_time(prev, pos)), ("meridian", meridian_time(prev, pos))):
                if at is not None and (place not in best or abs(at - p["ts"]) < abs(best[place] - p["ts"])):
                    best[place] = at
        at = timing_point(p["route"], best, p["ts"])
        if at is not None:
            if at != p["porkkala_ts"]:
                store.set_porkkala_ts(p["id"], at)
            done += 1
    return done
