"""Digitraffic AIS feed: one live MQTT-over-websocket connection plus REST lookups.

The live stream pushes positions as ships send them, so there is no polling.
REST is only used at startup (latest known positions) and for ship search.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import aiohttp
import paho.mqtt.client as mqtt

from .config import USER_AGENT
from .store import Store
from .tracker import Tracker

_LOGGER = logging.getLogger(__name__)

HOST = "meri.digitraffic.fi"
REST = f"https://{HOST}/api/ais/v1"
HEADERS = {"Digitraffic-User": "ha-ferry-watch", "User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
VESSEL_LIST_TTL = 24 * 3600


class DigitrafficFeed:
    def __init__(self, loop: asyncio.AbstractEventLoop, session: aiohttp.ClientSession,
                 store: Store, tracker: Tracker):
        self.loop = loop
        self.session = session
        self.store = store
        self.tracker = tracker
        self.client: mqtt.Client | None = None
        self.subscribed: set[int] = set()
        self.connected = False
        self.last_message_ts: float | None = None
        self.message_count = 0
        self._vessels: list[dict[str, Any]] = []
        self._vessels_ts = 0.0

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        await self.bootstrap()
        self._start_mqtt()

    def stop(self) -> None:
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "last_message_ts": self.last_message_ts,
            "message_count": self.message_count,
        }

    # --- MQTT ------------------------------------------------------------

    def _start_mqtt(self) -> None:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"ha-ferry-watch; {uuid.uuid4()}",
            transport="websockets",
        )
        client.tls_set()
        client.reconnect_delay_set(min_delay=2, max_delay=120)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_async(HOST, 443, keepalive=60)
        client.loop_start()
        self.client = client

    def _topics(self, mmsi: int) -> str:
        # Both "location" and "metadata" live under the ship's MMSI.
        return f"vessels-v2/{mmsi}/+"

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            _LOGGER.warning("Digitraffic refused connection: %s", reason_code)
            return
        self.connected = True
        _LOGGER.info("Connected to Digitraffic live AIS")
        self.subscribed = set()
        self.loop.call_soon_threadsafe(self.sync_subscriptions)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = False
        _LOGGER.warning("Disconnected from Digitraffic (%s), reconnecting", reason_code)

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        parts = msg.topic.split("/")
        if len(parts) != 3:
            return
        try:
            mmsi = int(parts[1])
            data = json.loads(msg.payload)
        except ValueError:
            return
        kind = parts[2]
        self.loop.call_soon_threadsafe(self._dispatch, mmsi, kind, data)

    def _dispatch(self, mmsi: int, kind: str, data: dict[str, Any]) -> None:
        self.last_message_ts = time.time()
        self.message_count += 1
        if kind.startswith("location"):
            self.tracker.on_location(mmsi, data)
        elif kind == "metadata":
            self.tracker.on_metadata(mmsi, data)

    def sync_subscriptions(self) -> None:
        """Match MQTT subscriptions to the configured ships."""
        if not self.client or not self.connected:
            return
        wanted = set(self.tracker.ships)
        for mmsi in self.subscribed - wanted:
            self.client.unsubscribe(self._topics(mmsi))
        for mmsi in wanted - self.subscribed:
            self.client.subscribe(self._topics(mmsi), qos=0)
        self.subscribed = wanted

    # --- REST ------------------------------------------------------------

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        async with self.session.get(url, params=params, headers=HEADERS,
                                    timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def bootstrap(self, mmsis: list[int] | None = None) -> None:
        """Load latest known positions so the map is not empty after a restart."""
        await self.resolve_reflagged()
        for mmsi in mmsis or list(self.tracker.ships):
            try:
                data = await self._get_json(f"{REST}/locations", {"mmsi": mmsi})
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Could not fetch latest position for %s: %s", mmsi, err)
                continue
            for feature in data.get("features", []):
                msg = rest_feature_to_location(feature)
                if msg:
                    self.tracker.on_location(mmsi, msg)
            try:
                meta = await self._get_json(f"{REST}/vessels/{mmsi}")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
            if isinstance(meta, dict) and meta:
                self.tracker.on_metadata(mmsi, meta)

    async def vessels(self) -> list[dict[str, Any]]:
        """All vessel metadata Digitraffic knows (cached for a day)."""
        if self._vessels and time.time() - self._vessels_ts < VESSEL_LIST_TTL:
            return self._vessels
        data = await self._get_json(f"{REST}/vessels")
        if isinstance(data, list):
            self._vessels = data
            self._vessels_ts = time.time()
        return self._vessels

    async def search(self, query: str, limit: int = 15) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        try:
            vessels = await self.vessels()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Vessel search failed: %s", err)
            return []
        q = query.lower()
        results = []
        for v in vessels:
            name = (v.get("name") or "").strip()
            mmsi = v.get("mmsi")
            if q.isdigit() and str(mmsi).startswith(q) or q in name.lower():
                results.append({
                    "mmsi": mmsi,
                    "name": name.title() if name.isupper() else name,
                    "destination": (v.get("destination") or "").strip() or None,
                    "ship_type": v.get("shipType"),
                })
        # Exact and prefix matches first.
        results.sort(key=lambda r: (r["name"].lower() != q, not r["name"].lower().startswith(q), r["name"]))
        return results[:limit]

    async def resolve_reflagged(self) -> None:
        """Fix default ships whose MMSI has no data by matching their exact name."""
        pending = [s for s in self.store.ships()
                   if s["auto_resolve"] and self.store.last_position(s["mmsi"]) is None]
        if not pending:
            return
        missing = []
        for ship in pending:
            try:
                data = await self._get_json(f"{REST}/locations", {"mmsi": ship["mmsi"]})
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return  # feed unreachable, try again next start
            if not data.get("features"):
                missing.append(ship)
        if not missing:
            return
        try:
            vessels = await self.vessels()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return
        for ship in missing:
            matches = [v for v in vessels
                       if (v.get("name") or "").strip().lower() == ship["name"].lower()
                       and v.get("mmsi") != ship["mmsi"]]
            if len(matches) == 1:
                new = int(matches[0]["mmsi"])
                _LOGGER.warning("%s: MMSI %s has no data, switching to %s (matched by name)",
                                ship["name"], ship["mmsi"], new)
                self.store.change_mmsi(ship["mmsi"], new)
        self.tracker.ships.clear()
        self.tracker.load()


def rest_feature_to_location(feature: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a REST GeoJSON feature into the same shape as a live message."""
    try:
        lon, lat = feature["geometry"]["coordinates"][:2]
    except (KeyError, TypeError, ValueError):
        return None
    props = feature.get("properties") or {}
    ts_ms = props.get("timestampExternal")
    ts = int(ts_ms / 1000) if isinstance(ts_ms, (int, float)) else int(time.time())
    return {
        "lat": lat,
        "lon": lon,
        "time": ts,
        "sog": props.get("sog"),
        "cog": props.get("cog"),
        "heading": props.get("heading"),
    }
