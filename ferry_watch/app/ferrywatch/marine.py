"""Waves, sea temperature and sea level as colour layers over Finnish waters, for the next 48
hours.

Open-Meteo's marine API (free for non-commercial use, CC BY 4.0): wave height, direction
and period, swell, sea surface temperature and sea level from the European wave and ocean
models it serves, asked for on the wind layer's grid of about 300 points over the water,
100 points a request. One run at most every 3 hours, only while a panel shows one of the
layers, kept as one small binary file per hour.

Sea level here is the model's height of the sea surface against mean sea level (tide,
wind and air pressure together), in cm; FMI's mareographs (the Sea conditions pill) say
what it actually is at Helsinki.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import aiohttp

from .forecast import http_getters
from .overlays import Overlay, Overlays, RunBuilder
from .wind import GRID as WATER_GRID

_LOGGER = logging.getLogger(__name__)

RUN_ID = "marine"
URL = "https://marine-api.open-meteo.com/v1/marine"
PARAMS = {"wave": "wave_height", "wdir": "wave_direction", "period": "wave_period",
          "swell": "swell_wave_height", "sdir": "swell_wave_direction",
          "sst": "sea_surface_temperature", "level": "sea_level_height_msl"}
FIELDS = list(PARAMS)
# Tenths, but wave height in cm (one decimal of a metre is too coarse for a calm Gulf),
# and sea level in cm (from metres).
SCALE = [100.0, 1.0, 10.0, 100.0, 1.0, 10.0, 1.0]
CM = {"level"}  # metres in the answer, centimetres kept
HOURS = 48
RUN_EVERY = 3 * 3600
BATCH = 100
ATTRIBUTION = "Sea: Open-Meteo.com marine forecast (CC BY 4.0)"
GRID = list(WATER_GRID)
BOUNDS = (min(p[0] for p in GRID), min(p[1] for p in GRID), max(p[0] for p in GRID), max(p[1] for p in GRID))

GetJson = Callable[[str, dict[str, str]], Awaitable[Any]]


def add_answer(b: RunBuilder, first: int, answer: Any) -> None:
    places = answer if isinstance(answer, list) else [answer]
    for n, place in enumerate(places):
        hourly = place.get("hourly") or {}
        times = hourly.get("time") or []
        for j, (f, name) in enumerate(PARAMS.items()):
            column = hourly.get(name)
            if not column:
                continue
            for ts, v in zip(times, column):
                if v is not None and f in CM:
                    v = v * 100
                b.set(first + n, int(ts), j, v)


async def fetch_run(session: aiohttp.ClientSession | None, now: float,
                    get_json: GetJson | None = None) -> dict[str, Any]:
    if get_json is None:
        get_json = http_getters(session)[1]
    start = int(now // 3600 * 3600)
    b = RunBuilder([start + i * 3600 for i in range(HOURS + 1)], FIELDS, GRID, SCALE)
    for i in range(0, len(GRID), BATCH):
        batch = GRID[i:i + BATCH]
        params = {"latitude": ",".join(f"{p[0]:.2f}" for p in batch),
                  "longitude": ",".join(f"{p[1]:.2f}" for p in batch),
                  "hourly": ",".join(PARAMS.values()), "timeformat": "unixtime", "timezone": "GMT",
                  "forecast_hours": str(HOURS + 1), "cell_selection": "sea"}
        add_answer(b, i, await get_json(URL, params))
    # Waves are the first field: an hour counts if a third of the points have them (the
    # coarser models leave narrow bays without).
    return b.run(min_share=0.3, info={"source": "open-meteo"})


LAYERS = [
    ("waves", "Waves", ["wave", "wdir", "period", "swell", "sdir"], "#0369a1", "waves",
     "Significant wave height with moving swell lines in the direction the waves travel. Tap for period and swell."),
    ("sea-temp", "Sea temperature", ["sst"], "#0891b2", "seatemp",
     "Sea surface temperature from the ocean model."),
    ("sea-level", "Sea level", ["level"], "#4f46e5", "sealevel",
     "The sea surface against its mean level, in cm: high with westerly gales and low pressure, low in easterlies."),
]


def overlays(get_json: GetJson | None = None, **changes: Any) -> list[Overlay]:
    async def points(session: aiohttp.ClientSession | None, now: float) -> dict[str, Any]:
        return await fetch_run(session, now, get_json)

    out = [Overlay(id=RUN_ID, title="Marine model", kind="points", points=points, hidden=True, max_age=RUN_EVERY,
                   refresh=1800, attribution=ATTRIBUTION, bounds=BOUNDS, valid_for=3600, **changes)]
    for oid, title, fields, color, icon, note in LAYERS:
        out.append(Overlay(id=oid, title=title, kind="points", run_of=RUN_ID, fields=fields, color=color, icon=icon,
                           note=note, group="field", max_age=RUN_EVERY, refresh=1800, frame_ms=700, opacity=0.8,
                           min_zoom=3, bounds=BOUNDS, valid_for=3600, attribution=ATTRIBUTION))
    return out


def register(registry: Overlays, get_json: GetJson | None = None, **changes: Any) -> None:
    for o in overlays(get_json, **changes):
        registry.register(o)
