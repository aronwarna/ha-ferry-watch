"""The blue-green algae layers for the simulator (offline): SYKE's satellite classes drawn
as tiles in SYKE's own colours, its capabilities, tap values and Open311 reports, all made
up but in the real formats, for last summer's season.

Blooms are blobs that grow from July to mid August and drift a little each day, with cloud
holes where the satellite saw nothing. The tiles cover land too (the real ones don't
always quite keep off it at 300 m): the panel cuts them to the chart's water.
"""

from __future__ import annotations

import json
import math
import random
import time
from typing import Any

from . import algae
from .simoverlays import png

TILE_PX = 128
COLOURS = {1: (0x2B, 0x7E, 0xA8), 2: (0xFF, 0xFF, 0xC9), 3: (0xFF, 0xDC, 0x28), 4: (0xCD, 0x33, 0x01)}
# Bloom centres: lat, lon, radius (degrees of latitude), strength.
BLOOMS = [(59.82, 24.25, 0.14, 1.2), (59.6, 22.9, 0.22, 1.1), (60.05, 21.6, 0.12, 1.0), (59.9, 26.0, 0.18, 1.2),
          (59.3, 21.0, 0.3, 1.0), (60.13, 24.95, 0.05, 1.3), (59.93, 23.4, 0.06, 1.2)]


def season_days(now: float) -> list[int]:
    start, end = algae.season_window(now)
    # The satellite misses a day now and then (no pass, or all cloud).
    return [d for d in range(start, end + 1, algae.DAY) if (d // algae.DAY) % 5 != 3]


def strength(ts: int) -> float:
    """0 to 1 over the season: blooms peak late July to mid August."""
    doy = time.gmtime(ts).tm_yday
    return max(0.0, 1 - abs(doy - 215) / 35)


def index_at(ts: int, lat: float, lon: float) -> int:
    """The class at a place on a day: 0 cloud, 1 clear sea, 2-4 algae."""
    day = ts // algae.DAY
    s = strength(ts)
    k = math.cos(math.radians(lat))
    # Clouds: a few big soft holes that move each day.
    rnd = random.Random(day)
    for _ in range(3):
        clat, clon = rnd.uniform(59, 61), rnd.uniform(20, 28)
        if ((lon - clon) * k) ** 2 + (lat - clat) ** 2 < 0.35 ** 2:
            return 0
    v = 0.0
    for i, (blat, blon, r, st) in enumerate(BLOOMS):
        drift = 0.02 * ((day + i * 7) % 11 - 5)
        d2 = (((lon - blon - drift) * k) ** 2 + (lat - blat - drift / 3) ** 2) / (r * r)
        if d2 < 9:
            # Streaky, as blooms are: the wind lays them out in bands.
            v += st * math.exp(-d2) * (0.75 + 0.25 * math.sin((lat * 60 + lon * 25) + day))
    v *= s
    return 4 if v > 0.95 else 3 if v > 0.7 else 2 if v > 0.4 else 1


def algae_tile(ts: int, z: int, x: int, y: int) -> bytes:
    n = 2**z
    pixels = bytearray()
    for row in range(TILE_PX):
        merc = math.pi * (1 - 2 * (y + (row + 0.5) / TILE_PX) / n)
        lat = math.degrees(math.atan(math.sinh(merc)))
        for col in range(TILE_PX):
            lon = (x + (col + 0.5) / TILE_PX) / n * 360 - 180
            c = index_at(ts, lat, lon) if 58.5 < lat < 61.5 and 19 < lon < 30 else 0
            pixels.extend((*COLOURS[c], 255) if c else (0, 0, 0, 0))
    return png(TILE_PX, TILE_PX, bytes(pixels))


async def fake_get(url: str, params: Any) -> str:
    """SYKE's WMS: capabilities (the layer's days) and GetFeatureInfo."""
    req = str(params.get("REQUEST", ""))
    now = time.time()
    if req == "GetCapabilities":
        dates = ",".join(time.strftime("%Y-%m-%dT00:00:00.000Z", time.gmtime(d)) for d in season_days(now))
        return (f'<WMS_Capabilities><Capability><Layer><Layer queryable="1"><Name>{algae.LAYER}</Name>'
                f'<Dimension name="time" default="" units="ISO8601">{dates}</Dimension></Layer></Layer>'
                '</Capability></WMS_Capabilities>')
    if req == "GetFeatureInfo":
        s, w, nn, e = (float(v) for v in params["BBOX"].split(","))
        lat, lon = (s + nn) / 2, (w + e) / 2
        ts = _day(params["TIME"])
        return json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"algae_index": index_at(ts, lat, lon)}}]})
    raise ValueError(f"simulated SYKE: no {req}")


def _day(text: str) -> int:
    t = time.strptime(text[:10], "%Y-%m-%d")
    return int(time.mktime(t) - time.timezone) // algae.DAY * algae.DAY


# Report places along the coast: beaches, jetties, monitoring sites (a few on lakes, which
# the layer leaves out).
SITES = [
    (60.1561, 24.7839, "Toppelundin koiraranta", 0), (60.1497, 24.6547, "Kivenlahti beach", 0),
    (60.0912, 24.4389, "Porkkala, Rönnskär", 2), (60.1170, 24.9840, "Hietaniemi beach", 0),
    (60.1460, 25.1460, "Vuosaari", 1), (59.8240, 22.9690, "Hanko, Plagen", 2),
    (60.2270, 25.1810, "Vartiokylänlahti", 0), (59.9750, 23.4370, "Tammisaari, Ekenäs", 2),
    (60.3370, 26.9390, "Kotka, Sapokka", 1), (60.4520, 22.1600, "Turku, Ruissalo", 2),
    (60.1140, 23.7720, "Inkoo harbour", 0), (60.2020, 21.9080, "Nauvo", 1),
    (60.0350, 24.3610, "Kirkkonummi, Upinniemi", 0), (60.3870, 26.3700, "Pernaja", 2),
    (61.4980, 23.7610, "Pyhäjärvi, Tampere (lake)", 2), (62.2410, 25.7440, "Jyväsjärvi (lake)", 0),
]


async def fake_reports(url: str, params: Any) -> list[dict[str, Any]]:
    """Open311 records like SYKE's, for the asked dates."""
    def ts_of(text: str) -> int:
        return int(time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone)
    a, b = ts_of(params["start_date"]), ts_of(params["end_date"])
    out = []
    for day in range(a // algae.DAY, (b - 1) // algae.DAY + 1):
        rnd = random.Random(day * 31)
        for i, (lat, lon, name, kind) in enumerate(SITES):
            weekly = kind == 2
            if weekly and (day + i) % 7:
                continue
            if not weekly and rnd.random() > 0.35:
                continue
            ts = day * algae.DAY + rnd.randint(6 * 3600, 18 * 3600)
            if not a <= ts < b:
                continue
            sat = index_at(ts, lat, lon)
            level = max(0, min(3, (sat - 1 if sat else rnd.randint(0, 1)) + rnd.choice((-1, 0, 0, 1))))
            local = time.strftime("%Y-%m-%dT%H:%M:00.0000000", time.gmtime(ts + 3 * 3600))
            tags = (f"#_jwObservation, #_jwSiteID_{2000 + i}, #_jwMaintainer_expert, "
                    "#_jwMonitoring_Valtakunnallinen-leväseuranta") if weekly else "#_VesiFiObservation"
            note = rnd.choice(["", "", name, "Rannassa sinileväröyhtöjä", "Vesi kirkasta", "Hajanaisia hiutaleita"])
            out.append({
                "service_request_id": day * 100 + i, "status": "open", "service_name": "Sinilevätilanne",
                "service_code": algae.SERVICE, "lat": lat + rnd.uniform(-0.002, 0.002),
                "long": lon + rnd.uniform(-0.004, 0.004), "requested_datetime": local,
                "description": f"This observation was saved via Vesi.fi. {tags} "
                               f"{algae.LEVEL_KEY}:{level},Ecology_additionalinfo:{note}",
            })
    return out
