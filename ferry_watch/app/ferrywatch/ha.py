"""Publishes each ship to Home Assistant as a device_tracker state.

Updates are batched to at most one per ship per interval, so the HA recorder
isn't flooded the way per-message sensors would.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp

from .config import HA_API

_LOGGER = logging.getLogger(__name__)


def entity_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"device_tracker.ferry_watch_{slug}"


class HAPublisher:
    def __init__(self, session: aiohttp.ClientSession, token: str, interval: int, app):
        self.session = session
        self.token = token
        self.interval = max(15, interval)
        self.app = app  # FerryWatch, for ship info and photo URLs
        self.dirty: set[int] = set()

    def on_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind in ("position", "meta"):
            self.dirty.add(payload["mmsi"])

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            pending, self.dirty = self.dirty, set()
            for mmsi in pending:
                try:
                    await self.publish(mmsi)
                except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                    _LOGGER.debug("HA update for %s failed: %s", mmsi, err)
                    self.dirty.add(mmsi)

    async def publish(self, mmsi: int) -> None:
        ship = self.app.store.ship(mmsi)
        if not ship or mmsi not in self.app.tracker.ships:
            return
        summary = self.app.tracker.ship_summary(mmsi)
        pos = summary["position"]
        if not pos:
            return
        attrs: dict[str, Any] = {
            "friendly_name": ship["name"],
            "icon": "mdi:ferry",
            "source_type": "gps",
            "latitude": pos["lat"],
            "longitude": pos["lon"],
            "gps_accuracy": 25,
            "speed_kn": pos["sog"],
            "course": pos["cog"],
            "heading": pos["heading"],
            "destination": summary["destination"],
            "mmsi": mmsi,
            "last_report": pos["ts"],
        }
        photo = self.app.photos.remote_url(mmsi)
        if photo:
            attrs["entity_picture"] = photo
        state = "in_port" if summary["in_port"] else "under_way"
        async with self.session.post(
            f"{HA_API}/states/{entity_id(ship['name'])}",
            json={"state": state, "attributes": attrs},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
