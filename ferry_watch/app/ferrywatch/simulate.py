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
UTO_ROUTE = [(60.1630, 22.1380), (60.0900, 21.9500), (59.9700, 21.7600), (59.8600, 21.5300), (59.7830, 21.3700)]
VUOSAARI_WEST = [(60.1500, 25.2000), (60.0500, 25.1000), (59.8500, 24.7000), (59.7000, 23.8000), (59.5500, 22.5000)]

# Fleet ships for the Fleets tab (made-up MMSIs), plus the four ferries' fleets.
FLEET_MEMBERS = {
    276000101: {"fleet": "tallink", "name": "Megastar"},
    276000102: {"fleet": "tallink", "name": "MyStar"},
    276000103: {"fleet": "viking", "name": "Viking XPRS"},
    230000104: {"fleet": "finnlines", "name": "Finnstar"},
    230000105: {"fleet": "finnlines", "name": "Finnmaid"},
    230000106: {"fleet": "finferries", "name": "Utö"},
    230000107: {"fleet": "rescue", "name": "PV Turva"},
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
    (230000106, UTO_ROUTE, 30 * 60, -90 * 60, 11.0),
    (230000107, [(59.98, 24.60), (59.93, 24.30), (59.90, 23.90)], 10 * 60, -30 * 60, 8.0),
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
    # Reported speed picks up after leaving port and wanders a little, so the
    # speed-coloured track has something to show (positions still move evenly).
    elapsed = ts - depart_ts
    sog = 0.0 if done else round(speed * min(1.0, 0.3 + elapsed / 1500) + 1.5 * math.sin(elapsed / 700), 1)
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


async def simulate_aircraft(watcher, step: float = 5.0) -> None:
    """A FinnHEMS helicopter flying to Porkkala and a Border Guard plane on patrol."""
    start = time.time()
    while True:
        if watcher.wanted():
            t = time.time() - start
            heli_f = (t % 900) / 900
            a = (t % 1200) / 1200 * 2 * math.pi
            rows = [
                {"hex": "461f01", "r": "OH-HMV", "t": "H145", "category": "A7", "flight": "FINNHEMS10",
                 "lat": 60.317 + (59.98 - 60.317) * heli_f, "lon": 24.963 + (24.40 - 24.963) * heli_f,
                 "alt_baro": 1500, "gs": 130, "track": 235, "seen_pos": 1, "baro_rate": -640,
                 "desc": "AIRBUS HELICOPTERS H-145", "ownOp": "Skarven Helicopters Oy", "year": "2019"},
                {"hex": "461f02", "r": "OH-MVL", "t": "D228", "category": "A1", "flight": "FGU2",
                 "lat": 59.85 + 0.12 * math.sin(a), "lon": 23.9 + 0.5 * math.cos(a),
                 "alt_baro": 1000, "gs": 150, "track": (math.degrees(-a) + 360) % 360, "seen_pos": 2,
                 "ownOp": "SUOMEN RAJAVARTIOLAITOS"},
            ]
            watcher.update(rows, time.time())
            watcher.publish(watcher.snapshot())
        await asyncio.sleep(step)


# --- a made-up archipelago for the vector chart ----------------------------------------

SIM_ISLANDS = [  # lon, lat, radius in metres
    (24.40, 60.005, 1400), (24.47, 60.02, 900), (24.33, 60.03, 1100), (24.55, 59.99, 600),
    (24.25, 59.995, 800), (24.60, 60.03, 1300), (24.43, 59.965, 450), (24.70, 60.00, 700),
    (24.36, 59.975, 350), (24.52, 60.045, 500),
]
# Depth bands as multiples of an island's radius: shore, 3 m, 6 m, 10 m.
SIM_BANDS = (1.0, 1.25, 1.5, 1.8)


def _spread_islands(islands):
    """Drop islands whose depth bands would overlap a bigger one's (the made-up depth
    areas must not overlap, like real ones)."""
    from .vchart import lonlat_to_mercator
    kept = []
    for lon, lat, r in sorted(islands, key=lambda i: -i[2]):
        x, y = lonlat_to_mercator(lon, lat)
        if all(math.hypot(x - kx, y - ky) > (r + kr) * 2 * SIM_BANDS[-1] * 1.4 for kx, ky, kr in kept):
            kept.append((x, y, r))
            yield lon, lat, r


SIM_ISLANDS = list(_spread_islands(SIM_ISLANDS))
SIM_FAIRWAY = [(24.95, 60.05), (24.62, 59.975), (24.38, 59.945), (24.0, 59.935)]
SIM_BOAT_ROUTE = [(24.37, 60.06), (24.395, 60.032), (24.445, 60.0), (24.5, 59.98)]
SIM_SMALL_ROUTE = [(24.5, 59.98), (24.53, 60.0), (24.56, 60.03)]
# Wrecks and underwater sites: lon, lat, register id, name, class, sub-type, dating, accuracy
SIM_HERITAGE = [
    (24.416, 59.930, 1181, "Salmö 2", 0, "hylyt (puu)", "historiallinen", "Ohjeellinen (10 - 100 m)"),
    (24.58, 60.0005, 1203, "Falken", 0, "hylyt (puu)", "historiallinen", "Tarkka (< 10 m)"),
    (24.52, 59.990, 1310, "Alku", 1, "hylyt (metalli)", "moderni", "Tarkka (< 10 m)"),
    (24.455, 59.975, 1422, None, 3, "hylyt (puu)", "ajoittamaton", "Suuntaa antava (100 - 1000 m)"),
    (24.48, 60.012, 1501, "Ankkurilöytö", 4, "löytöpaikat", "historiallinen", "Ohjeellinen (10 - 100 m)"),
]


def _sim_ring(lon: float, lat: float, radius: float, scale: float, wobble: float = 0.22):
    from .vchart import lonlat_to_mercator
    cx, cy = lonlat_to_mercator(lon, lat)
    r = radius * 2  # metres -> web-mercator metres at 60°N
    seed = lon * 97 + lat * 31
    pts = []
    for i in range(48):
        a = 2 * math.pi * i / 48
        f = 1 + wobble * math.sin(3 * a + seed) + 0.5 * wobble * math.sin(7 * a + 2 * seed)
        pts.append([cx + r * scale * f * math.cos(a), cy + r * scale * f * math.sin(a)])
    pts.append(pts[0])
    return pts


def _sim_line(points):
    from .vchart import lonlat_to_mercator
    return [list(lonlat_to_mercator(lon, lat)) for lon, lat in points]


async def fake_chart_layer(key: str, bbox) -> list[dict]:
    """Stand-in for the WFS services: islands with depth bands, contours, soundings,
    a fairway with buoys and a boat route."""
    import random
    minx, miny, maxx, maxy = bbox
    b = SIM_BANDS
    bands = [(0, 3, b[0], b[1]), (3, 6, b[1], b[2]), (6, 10, b[2], b[3])]
    feats: list[dict] = []
    if key == "depth":
        outer = [[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]
        feats.append({"geometry": {"type": "Polygon", "coordinates": [outer] + [
            _sim_ring(lon, lat, r, b[3]) for lon, lat, r in SIM_ISLANDS]},
            "properties": {"DRVAL1": 10, "DRVAL2": 50}})
        for d1, d2, inner, outer_s in bands:
            for lon, lat, r in SIM_ISLANDS:
                feats.append({"geometry": {"type": "Polygon", "coordinates": [
                    _sim_ring(lon, lat, r, outer_s), _sim_ring(lon, lat, r, inner)]},
                    "properties": {"DRVAL1": d1, "DRVAL2": d2}})
    elif key == "contour":
        for depth, s in ((3, SIM_BANDS[1]), (6, SIM_BANDS[2]), (10, SIM_BANDS[3])):
            for lon, lat, r in SIM_ISLANDS:
                feats.append({"geometry": {"type": "LineString", "coordinates": _sim_ring(lon, lat, r, s)},
                              "properties": {"VALDCO": depth}})
    elif key == "sounding":
        rnd = random.Random(int(minx) ^ int(miny))
        for _ in range(400):
            x, y = rnd.uniform(minx, maxx), rnd.uniform(miny, maxy)
            feats.append({"geometry": {"type": "Point", "coordinates": [x, y]},
                          "properties": {"DEPTH": round(rnd.uniform(4, 60), 1)}})
    elif key == "fline":
        feats.append({"geometry": {"type": "LineString", "coordinates": _sim_line(SIM_FAIRWAY)},
                      "properties": {"mitoitussyvays": 10.0, "jnro": 4070}})
        feats.append({"geometry": {"type": "LineString", "coordinates": _sim_line(SIM_BOAT_ROUTE)},
                      "properties": {"mitoitussyvays": 2.4, "jnro": 4007}})
        feats.append({"geometry": {"type": "LineString", "coordinates": _sim_line(SIM_SMALL_ROUTE)},
                      "properties": {"mitoitussyvays": None, "jnro": 4050}})
    elif key == "route":
        for line, vl, jnro in ((SIM_FAIRWAY, 1, 4070), (SIM_BOAT_ROUTE, 4, 4007), (SIM_SMALL_ROUTE, 6, 4050)):
            feats.append({"geometry": {"type": "LineString", "coordinates": _sim_line(line)},
                          "properties": {"vaylaluokkakoodi": vl, "jnro": jnro}})
    elif key == "farea":
        line = _sim_line(SIM_FAIRWAY)
        left = [[x, y + 700] for x, y in line]
        right = [[x, y - 700] for x, y in line][::-1]
        feats.append({"geometry": {"type": "Polygon", "coordinates": [left + right + [left[0]]]},
                      "properties": {"tyyppikoodi": 1, "mitoitussyvays": 10.0, "jnro": 4070}})
        feats.append({"geometry": {"type": "Polygon", "coordinates": [_sim_ring(24.53, 60.015, 300, 1.0, 0.05)]},
                      "properties": {"tyyppikoodi": 2, "mitoitussyvays": None, "jnro": None}})
    elif key == "restrict":
        feats.append({"geometry": {"type": "Polygon", "coordinates": [_sim_ring(24.385, 60.02, 600, 1.0, 0.1)]},
                      "properties": {"rajoitustyypit": "01", "suuruus": 10}})
    elif key == "lead":
        feats.append({"geometry": {"type": "LineString", "coordinates": _sim_line([(24.66, 59.99), (24.62, 59.975), (24.52, 59.938)])},
                      "properties": {"suunta": 234.8}})
    elif key == "sector":
        from .vchart import lonlat_to_mercator
        x, y = lonlat_to_mercator(24.62, 59.975)
        for a, b, colour in ((200, 238, "vi"), (238, 246, "v"), (246, 290, "p"), (40, 70, "v")):
            feats.append({"geometry": {"type": "Point", "coordinates": [x, y]},
                          "properties": {"alkukulma": a, "loppukulma": b, "vari": colour}})
        # Plain lights have one all-round "sector" that gives their colour.
        for lon, lat, colour in ((24.40, 60.005, "v"), (24.80, 60.020, "vi"), (24.66, 59.99, "v"), (24.70, 59.992, "v")):
            feats.append({"geometry": {"type": "Point", "coordinates": list(lonlat_to_mercator(lon, lat))},
                          "properties": {"alkukulma": 0, "loppukulma": 360, "vari": colour}})
    elif key == "aid":
        from .vchart import lonlat_to_mercator
        aids = [
            (24.80, 60.030, "Viitta", 1, "E", "Rönnskär 1"), (24.80, 60.020, "Viitta", 2, "K", "Rönnskär 2"),
            (24.70, 59.992, "Poiju", 3, "K", "Kallbåda N"), (24.55, 59.968, "Viitta", 4, "E", "Stora S"),
            (24.47, 59.958, "Viitta", 5, "E", "Porkala W"), (24.30, 59.952, "Viitta", 6, "E", "Rönn E"),
            (24.40, 60.005, "Merimajakka", 99, "K", "Porkkala majakka"),
            (24.62, 59.975, "Sektoriloisto", 99, "K", "Kallbådan"), (24.66, 59.99, "Linjamerkki", 99, "K", "Linja ylempi"),
            (24.43, 59.965, "Kummeli", 99, "E", "Kummeli"), (24.395, 60.032, "Viitta", 7, "E", "Kari"),
            (24.445, 60.0, "Viitta", 8, "E", "Turvavesi"), (24.5, 59.98, "Poiju", 9, "E", "Erikois"),
            (24.36, 59.975, "Reunamerkki", 2, "K", "Reuna"),
        ]
        for lon, lat, kind, cat, lit, name in aids:
            feats.append({"geometry": {"type": "MultiPoint", "coordinates": [list(lonlat_to_mercator(lon, lat))]},
                          "properties": {"turvalaitetyyppifi": kind, "navigointilajikoodi": cat,
                                         "valaistu": lit, "nimifi": name}})
    elif key == "light":
        from .vchart import lonlat_to_mercator
        for lon, lat, text in ((24.40, 60.005, "LFl(2) 15s"), (24.62, 59.975, "Iso WRG 4s"),
                               (24.66, 59.99, "Q"), (24.80, 60.020, "Fl G 3s")):
            feats.append({"geometry": {"type": "Point", "coordinates": list(lonlat_to_mercator(lon, lat))},
                          "properties": {"viralvalotunnuse": text}})
    elif key == "shore":
        feats.append({"geometry": {"type": "LineString", "coordinates": _sim_line([(24.3905, 60.0115), (24.388, 60.016)])},
                      "properties": {}})
    elif key == "rock":
        import math
        from .vchart import lonlat_to_mercator
        rnd = random.Random(int(minx) ^ int(miny) ^ 7)
        for lon, lat, r in SIM_ISLANDS:
            cx, cy = lonlat_to_mercator(lon, lat)
            for _ in range(int(r / 60)):
                a = rnd.uniform(0, 2 * math.pi)
                d = 2 * r * rnd.uniform(SIM_BANDS[0] * 1.05, SIM_BANDS[2])
                feats.append({"geometry": {"type": "Point", "coordinates": [cx + d * math.cos(a), cy + d * math.sin(a)]},
                              "properties": {"kohdeluokka": rnd.choice((38511, 38511, 38512, 38513))}})
    elif key == "rockarea":
        feats.append({"geometry": {"type": "Polygon", "coordinates": [_sim_ring(24.505, 60.005, 250, 1.0)]},
                      "properties": {"kohdeluokka": 38511}})
    elif key == "heritage":
        from .vchart import lonlat_to_mercator
        for lon, lat, site, name, cls, sub, dating, acc in SIM_HERITAGE:
            x, y = lonlat_to_mercator(lon, lat)
            if minx <= x <= maxx and miny <= y <= maxy:
                feats.append({"geometry": {"type": "Point", "coordinates": [x, y]},
                              "properties": {"mjtunnus": site, "kohdenimi": name, "_class": cls,
                                             "tyyppi": "alusten hylyt" if "hylyt" in sub else sub,
                                             "alatyyppi": sub, "ajoitus": dating, "paikannustarkkuus": acc}})
    elif key == "wreck":
        from .vchart import lonlat_to_mercator
        for lon, lat, code in ((24.58, 60.0, 16721), (24.35, 60.0, 16722), (24.49, 59.995, 16712)):
            feats.append({"geometry": {"type": "Point", "coordinates": list(lonlat_to_mercator(lon, lat))},
                          "properties": {"kohdeluokka": code}})
    elif key == "name":
        from .vchart import lonlat_to_mercator
        names = [(24.40, 60.005, "Rönnskär", "swe", 35070), (24.40, 60.003, "Rönnskari", "fin", 35070),
                 (24.47, 60.02, "Stora Fågelskär", "swe", 35070), (24.33, 60.03, "Långholmen", "swe", 35070),
                 (24.60, 60.03, "Porkkala", "fin", 48120), (24.50, 59.94, "Porkkalanselkä", "fin", 36490),
                 (24.25, 59.995, "Kallbådan", "swe", 35070), (24.70, 60.00, "Mäkiluoto", "fin", 35070)]
        for lon, lat, text, lang, code in names:
            feats.append({"geometry": {"type": "Point", "coordinates": list(lonlat_to_mercator(lon, lat))},
                          "properties": {"teksti": text, "kieli": lang, "kohdeluokka": code}})
    return feats
