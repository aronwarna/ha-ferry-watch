"""Home Assistant access: ship device_trackers, phone location and notifications."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from . import config
from .passages import DIRECTION_LABELS

_LOGGER = logging.getLogger(__name__)

LOCATION_REQUEST_WAIT = 60  # seconds to wait for the phone to answer a location request
STALE_LOCATION_LIMIT = 2 * 3600  # an old fix is still used if the phone doesn't answer


def entity_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"device_tracker.ferry_watch_{slug}"


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class HAClient:
    """Thin client for the Core API through the Supervisor proxy."""

    def __init__(self, session: aiohttp.ClientSession, token: str):
        self.session = session
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout = aiohttp.ClientTimeout(total=20)

    async def get_state(self, entity: str) -> dict[str, Any] | None:
        async with self.session.get(f"{config.HA_API}/states/{entity}", headers=self.headers,
                                    timeout=self.timeout) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            return await resp.json()

    async def set_state(self, entity: str, state: str, attributes: dict[str, Any]) -> None:
        async with self.session.post(f"{config.HA_API}/states/{entity}", headers=self.headers,
                                     json={"state": state, "attributes": attributes},
                                     timeout=self.timeout) as resp:
            resp.raise_for_status()

    async def call_service(self, service: str, data: dict[str, Any]) -> None:
        domain, _, name = service.partition(".")
        async with self.session.post(f"{config.HA_API}/services/{domain}/{name}", headers=self.headers,
                                     json=data, timeout=self.timeout) as resp:
            resp.raise_for_status()

    async def fire_event(self, event_type: str, data: dict[str, Any]) -> None:
        async with self.session.post(f"{config.HA_API}/events/{event_type}", headers=self.headers,
                                     json=data, timeout=self.timeout) as resp:
            resp.raise_for_status()

    async def phone_location(self, entity: str, notify_service: str,
                             max_age: float) -> tuple[float, float, float] | None:
        """(lat, lon, age in seconds) of the phone, asking it for a fresh fix if needed."""
        fix = await self._phone_fix(entity)
        if fix and fix[2] <= max_age:
            return fix
        # Ask the companion app for a new location and wait briefly for it.
        try:
            await self.call_service(notify_service, {"message": "request_location_update"})
        except aiohttp.ClientError as err:
            _LOGGER.warning("Could not ask the phone for its location: %s", err)
        deadline = time.monotonic() + LOCATION_REQUEST_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            fresh = await self._phone_fix(entity)
            if fresh and fresh[2] <= max_age:
                return fresh
        if fix and fix[2] <= STALE_LOCATION_LIMIT:
            _LOGGER.info("Phone didn't send a new location; using one from %.0f min ago", fix[2] / 60)
            return fix
        return None

    async def _phone_fix(self, entity: str) -> tuple[float, float, float] | None:
        try:
            state = await self.get_state(entity)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Could not read %s: %s", entity, err)
            return None
        fix = _fix(state)
        return (fix["lat"], fix["lon"], time.time() - fix["ts"]) if fix else None

    async def phone_state(self, entity: str) -> dict[str, Any] | None:
        """The phone's last reported position for the map ({lat, lon, ts, accuracy}), without
        asking it for a new one. Quiet on errors: the panel asks every minute."""
        try:
            return _fix(await self.get_state(entity))
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Could not read %s: %s", entity, err)
            return None


def _fix(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    attrs = state.get("attributes", {})
    lat, lon = attrs.get("latitude"), attrs.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    # last_reported moves even when the phone reports the same position again.
    seen = [t for t in (_parse_ts(state.get("last_reported")), _parse_ts(state.get("last_updated"))) if t]
    accuracy = attrs.get("gps_accuracy")
    return {"lat": float(lat), "lon": float(lon), "ts": max(seen) if seen else 0.0,
            "accuracy": accuracy if isinstance(accuracy, (int, float)) else None}


NEXT_PASSAGE_ENTITY = "sensor.ferry_watch_next_passage"
NEXT_PASSAGE_SLACK = 120  # seconds the predicted time may move before the sensor is sent again


class NextPassageSensor:
    """sensor.ferry_watch_next_passage: the soonest predicted passage of a ship with alerts
    on (a timestamp, "unknown" when none is coming). Sent only when the ship or direction
    changes or the time moves more than NEXT_PASSAGE_SLACK, so the recorder isn't flooded."""

    def __init__(self, client):
        self.client = client
        self.sent: tuple | None | bool = False  # False: nothing sent yet

    def _due(self, key: tuple | None) -> bool:
        if self.sent is False:
            return True
        if key is None or self.sent is None:
            return key != self.sent
        return key[:2] != self.sent[:2] or abs(key[2] - self.sent[2]) > NEXT_PASSAGE_SLACK

    async def update(self, ship: str | None, mmsi: int | None, prediction: dict[str, Any] | None,
                     force: bool = False) -> bool:
        key = (mmsi, prediction["direction"], prediction["eta_ts"]) if prediction else None
        if not force and not self._due(key):
            return False
        attrs: dict[str, Any] = {"friendly_name": "Ferry Watch next passage", "device_class": "timestamp",
                                 "icon": "mdi:ferry"}
        state = "unknown"
        if prediction:
            state = datetime.fromtimestamp(prediction["eta_ts"], timezone.utc).isoformat()
            attrs.update({"ship": ship, "mmsi": mmsi,
                          "direction": DIRECTION_LABELS.get(prediction["direction"], prediction["direction"])})
            if prediction.get("of"):
                attrs.update({"inner_route": prediction["inner"], "sailings": prediction["of"]})
        await self.client.set_state(NEXT_PASSAGE_ENTITY, state, attrs)
        self.sent = key
        return True


class HAPublisher:
    """Publishes each ship as a device_tracker state, at most once per ship per interval,
    so the HA recorder isn't flooded the way per-message sensors would."""

    def __init__(self, client: HAClient, interval: int, app):
        self.client = client
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
        await self.client.set_state(entity_id(ship["name"]), state, attrs)
