"""Offline simulator for development (FW_SIMULATE=1).

Feeds made-up but plausible ferry movements through the normal tracker, so
the panel can be developed and tested without the live Digitraffic feed.
Never used in a normal install.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import Any

from .geo import distance_m
from .store import Store
from .tracker import Tracker

_LOGGER = logging.getLogger(__name__)

KNOTS = 0.514444

HELSINKI_OLYMPIA = (60.1600, 24.9560)
HELSINKI_KATAJANOKKA = (60.1650, 24.9660)
OUT_OF_HELSINKI = [(60.1420, 24.9840), (60.1200, 24.9900)]
INNER_ROUTE = [(60.1100, 24.8500), (60.0900, 24.6800), (60.0500, 24.5200), (59.9900, 24.3600),
               (59.9500, 24.1000), (59.8800, 23.8000), (59.8000, 23.4500), (59.7600, 23.0500),
               (59.7200, 22.7000), (59.6500, 22.0000)]
OUTER_ROUTE = [(60.0500, 24.9500), (59.9500, 24.7000), (59.8600, 24.4000), (59.8000, 24.0500),
               (59.7500, 23.6000), (59.7000, 23.1000), (59.6500, 22.4000)]

# (mmsi, path, seconds docked before departing, departure relative to now, speed kn)
SCENARIOS = [
    (230184000, [HELSINKI_OLYMPIA, *OUT_OF_HELSINKI, *INNER_ROUTE], 90 * 60, -150 * 60, 17.0),
    (265004000, [*reversed(INNER_ROUTE), *reversed(OUT_OF_HELSINKI), HELSINKI_OLYMPIA], 0, -200 * 60, 16.5),
    (230713000, [HELSINKI_KATAJANOKKA], 10**6, 0, 0.0),
    (230361000, [HELSINKI_KATAJANOKKA, *OUT_OF_HELSINKI, *OUTER_ROUTE], 60 * 60, -95 * 60, 18.5),
]

HELSINKI_WEST = (60.1560, 24.9200)
TALLINN = [(60.1300, 24.9100), (60.0500, 24.8800), (59.8000, 24.8300), (59.5500, 24.7800), (59.4440, 24.7700)]
VUOSAARI = (60.2050, 25.1900)
VUOSAARI_WEST = [(60.1500, 25.2000), (60.0500, 25.1000), (59.8500, 24.7000), (59.7000, 23.8000), (59.5500, 22.5000)]

# Fleet ships for the Fleets tab (made-up MMSIs), plus the four ferries' fleets.
FLEET_MEMBERS = {
    276000101: {"fleet": "tallink", "name": "Megastar"},
    276000102: {"fleet": "tallink", "name": "MyStar"},
    276000103: {"fleet": "viking", "name": "Viking XPRS"},
    230000104: {"fleet": "finnlines", "name": "Finnstar"},
    230000105: {"fleet": "finnlines", "name": "Finnmaid"},
    230184000: {"fleet": "tallink", "name": "Silja Serenade"},
    265004000: {"fleet": "tallink", "name": "Silja Symphony"},
    230713000: {"fleet": "viking", "name": "Viking Cinderella"},
    230361000: {"fleet": "viking", "name": "Gabriella"},
}
FLEET_SCENARIOS = [
    (276000101, [HELSINKI_WEST, *TALLINN], 60 * 60, -40 * 60, 25.0),
    (276000102, [*reversed(TALLINN), HELSINKI_WEST], 60 * 60, -70 * 60, 25.0),
    (276000103, [TALLINN[-1], *reversed(TALLINN[:-1]), HELSINKI_KATAJANOKKA], 45 * 60, -20 * 60, 22.0),
    (230000104, [VUOSAARI, *VUOSAARI_WEST], 2 * 3600, -180 * 60, 21.0),
    (230000105, [VUOSAARI], 10**6, 0, 0.0),
]


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    x = math.sin(dl) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _point_along(path, metres: float):
    """Position, bearing and whether the end was reached, `metres` along the path."""
    for a, b in zip(path, path[1:]):
        seg = distance_m(*a, *b)
        if metres <= seg:
            f = metres / seg
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f), _bearing(a, b), False
        metres -= seg
    end = path[-1]
    brg = _bearing(path[-2], path[-1]) if len(path) > 1 else 0.0
    return end, brg, True


def _message(path, dock_s: int, depart_ts: float, speed: float, ts: float) -> dict:
    if ts < depart_ts or len(path) == 1:
        lat, lon = path[0]
        return {"time": int(ts), "lat": lat, "lon": lon, "sog": 0.0, "cog": 0.0, "heading": 180}
    (lat, lon), brg, done = _point_along(path, (ts - depart_ts) * speed * KNOTS)
    sog = 0.0 if done else speed
    return {"time": int(ts), "lat": lat, "lon": lon, "sog": sog, "cog": round(brg, 1),
            "heading": int(brg)}


async def run_simulation(tracker: Tracker, fleet_tracker: Tracker | None = None,
                         backfill_hours: float = 4.0, step: float = 3.0) -> None:
    start = time.time()
    plans = [(tracker, m, p, d, start + rel, s) for m, p, d, rel, s in SCENARIOS]
    if fleet_tracker:
        plans += [(fleet_tracker, m, p, d, start + rel, s) for m, p, d, rel, s in FLEET_SCENARIOS
                  if m in fleet_tracker.ships]
    metas = {
        230184000: "STOCKHOLM", 265004000: "HELSINKI", 230713000: "STOCKHOLM", 230361000: "STOCKHOLM",
    }
    for mmsi, dest in metas.items():
        tracker.on_metadata(mmsi, {"destination": dest, "timestamp": int(start * 1000)})

    # Backfill history in 10 s steps (only the docked period before departure counts as port).
    ts = start - backfill_hours * 3600
    while ts < start:
        for t, mmsi, path, dock_s, depart, speed in plans:
            if ts >= depart - dock_s or len(path) == 1:
                t.on_location(mmsi, _message(path, dock_s, depart, speed, ts))
        ts += 10
    while True:
        now = time.time()
        for t, mmsi, path, dock_s, depart, speed in plans:
            t.on_location(mmsi, _message(path, dock_s, depart, speed, now))
        await asyncio.sleep(step)


def seed_history(store: Store, days: int = 290) -> None:
    """Made-up passages for trying the stats view (FW_SIM_HISTORY=1). Each night one Silja
    and one Viking ship leaves Helsinki and the other comes back in the morning; about
    half the trips use the inner route."""
    if store.passages(limit=1):
        return
    rng = random.Random(7)
    pairs = [(230184000, 265004000), (230361000, 230713000)]
    start = time.time() - days * 86400
    for day in range(days):
        midnight = start - start % 86400 + day * 86400 - 3 * 3600  # roughly local midnight in Finland
        for out_ship, in_ship in pairs:
            if day % 2:
                out_ship, in_ship = in_ship, out_ship
            for mmsi, direction, hour in ((in_ship, "east", 8.6), (out_ship, "west", 17.4)):
                ts = int(midnight + (hour + rng.uniform(-0.6, 0.6)) * 3600)
                if ts > time.time() - 3600:
                    continue
                route = "inner" if rng.random() < 0.55 else "outer"
                pid = store.add_passage({"mmsi": mmsi, "ts": ts, "route": route, "direction": direction,
                                         "zone_id": None, "lat": 59.98, "lon": 24.4, "sog": 17.0})
                store.set_passage_result(pid, False, "made-up history")


class FakeHA:
    """Stands in for Home Assistant during development (FW_FAKE_HA=1): the phone sits
    on the coast at Porkkala and notifications are written to the log."""

    def __init__(self, phone: tuple[float, float] = (60.115, 24.45)):
        self.phone = phone
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.states: dict[str, tuple[str, dict[str, Any]]] = {}

    async def phone_location(self, entity: str, notify_service: str, max_age: float):
        return self.phone[0], self.phone[1], 30.0

    async def call_service(self, service: str, data: dict[str, Any]) -> None:
        self.calls.append((service, data))
        _LOGGER.info("FAKE %s: %s", service, data)

    async def set_state(self, entity: str, state: str, attributes: dict[str, Any]) -> None:
        self.states[entity] = (state, attributes)

    async def fire_event(self, event_type: str, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))
        _LOGGER.info("FAKE event %s: %s", event_type, data)
