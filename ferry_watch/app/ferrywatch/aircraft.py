"""Rescue and Border Guard aircraft from adsb.fi's open ADS-B data.

Only fetched while the panel is open and the Aircraft switch is on, so it costs
nothing the rest of the time. Positions are kept in memory with a short trail;
nothing is stored.

adsb.fi asks for personal, non-commercial use, at most one request a second,
and a credit with a link, which the panel shows.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

import aiohttp

from .config import USER_AGENT

_LOGGER = logging.getLogger(__name__)

# The Gulf of Finland and the Archipelago Sea, 160 nautical miles around Porkkala.
API_URL = "https://opendata.adsb.fi/api/v2/lat/60.0/lon/23.6/dist/160"
POLL_SECONDS = 10
TRAIL_POINTS = 60  # about 10 minutes
FORGET_SECONDS = 120
HELICOPTER_TYPES = {"EC35", "EC45", "H145", "EC30", "EC55", "A109", "A119", "A139", "A169", "AS32",
                    "AS50", "AS55", "AS65", "EC25", "EC75", "H160", "H215", "NH90", "S76", "S92", "B06",
                    "B407", "B429", "R44", "R66", "MD52", "MI8"}
# Border Guard registrations: helicopters OH-HV.., Dornier patrol planes OH-MV...
OPERATORS = (("OH-HV", "Border Guard"), ("OH-MV", "Border Guard"), ("OH-HP", "Police"))


def operator(reg: str, flight: str) -> str | None:
    for prefix, name in OPERATORS:
        if reg.startswith(prefix):
            return name
    if "HEMS" in flight.upper():
        return "FinnHEMS"
    return None


def interesting(ac: dict[str, Any]) -> bool:
    """Finnish helicopters (medical, Border Guard, police) and Border Guard planes."""
    reg = str(ac.get("r") or "").upper()
    if not reg.startswith("OH-"):
        return False
    return (ac.get("category") == "A7" or str(ac.get("t") or "").upper() in HELICOPTER_TYPES
            or operator(reg, str(ac.get("flight") or "")) is not None)


class AircraftWatcher:
    def __init__(self, session: aiohttp.ClientSession, wanted: Callable[[], bool],
                 publish: Callable[[list[dict[str, Any]]], None]):
        self.session = session
        self.wanted = wanted  # switch on and a panel open
        self.publish = publish
        self.aircraft: dict[str, dict[str, Any]] = {}
        self.error: str | None = None

    async def run(self) -> None:
        while True:
            if self.wanted():
                await self.poll()
            elif self.aircraft:
                self.aircraft.clear()
            await asyncio.sleep(POLL_SECONDS)

    async def poll(self) -> None:
        try:
            async with self.session.get(API_URL, headers={"User-Agent": USER_AGENT},
                                        timeout=aiohttp.ClientTimeout(total=8)) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            if self.error is None:
                _LOGGER.warning("Could not read aircraft from adsb.fi: %s", err)
            self.error = str(err) or type(err).__name__
            return
        self.error = None
        self.update(data.get("ac") or data.get("aircraft") or [], time.time())
        self.publish(self.snapshot())

    def update(self, rows: list[dict[str, Any]], now: float) -> None:
        for ac in rows:
            if not interesting(ac) or ac.get("lat") is None or ac.get("lon") is None:
                continue
            hexid = str(ac.get("hex") or ac["r"]).lower()
            reg = str(ac["r"]).upper()
            flight = str(ac.get("flight") or "").strip()
            alt = ac.get("alt_baro")
            seen = now - float(ac.get("seen_pos") or 0)
            entry = self.aircraft.setdefault(hexid, {"hex": hexid, "trail": []})
            entry.update({
                "reg": reg,
                "callsign": flight or None,
                "type": ac.get("t"),
                "operator": operator(reg, flight),
                "helicopter": ac.get("category") == "A7" or str(ac.get("t") or "").upper() in HELICOPTER_TYPES,
                "lat": ac["lat"], "lon": ac["lon"],
                "alt_ft": 0 if alt == "ground" else alt,
                "gs_kn": ac.get("gs"),
                "track": ac.get("track"),
                "ts": seen,
            })
            trail = entry["trail"]
            if not trail or trail[-1][:2] != [ac["lat"], ac["lon"]]:
                trail.append([ac["lat"], ac["lon"]])
                del trail[:-TRAIL_POINTS]
        for hexid in [h for h, e in self.aircraft.items() if now - e["ts"] > FORGET_SECONDS]:
            del self.aircraft[hexid]

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self.aircraft.values())
