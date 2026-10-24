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
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .geo import line_crossed, point_in_polygon
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

DIRECTION_LABELS = {"east": "towards Helsinki", "west": "towards Stockholm"}

PassageHandler = Callable[[dict[str, Any]], None]


def direction(prev: Position, pos: Position) -> str:
    if pos.lon != prev.lon:
        return "east" if pos.lon > prev.lon else "west"
    cog = pos.cog if pos.cog is not None else prev.cog
    return "east" if cog is not None and 0 < cog < 180 else "west"


def crosses_meridian(prev: Position, pos: Position) -> bool:
    """The move crosses (or ends on) the Porkkala meridian between the outer route and the coast."""
    m = PORKKALA_MERIDIAN
    if not ((prev.lon - m) * (pos.lon - m) < 0 or (pos.lon == m and prev.lon != m)):
        return False
    lat = prev.lat + (pos.lat - prev.lat) * (m - prev.lon) / (pos.lon - prev.lon)
    return MERIDIAN_LATS[0] <= lat <= MERIDIAN_LATS[1]


class Detector:
    def __init__(self, store: Store, on_passage: PassageHandler | None = None):
        self.store = store
        self.on_passage = on_passage
        self.prev: dict[int, Position] = {}
        self.area_run: dict[tuple[int, int], list[Position]] = {}
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
        if (self.auto_outer and prev and moving and pos.ts - prev.ts <= MAX_GAP and not any(found)
                and crosses_meridian(prev, pos)):
            inner = self.store.last_passage(pos.mmsi, "inner")
            if not inner or pos.ts - inner["ts"] >= COOLDOWN:
                found.append(self._record(AUTO_OUTER, prev, pos))
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
            self.store.delete_auto_outer(pos.mmsi, pos.ts - COOLDOWN)
        passage["id"] = self.store.add_passage(passage)
        _LOGGER.info("%s: %s route passage %s", pos.mmsi, passage["route"], passage["direction"])
        if self.on_passage:
            self.on_passage(passage)
        return passage
