"""Digitraffic AIS feed: one live MQTT-over-websocket connection plus REST lookups.

The live stream pushes positions as ships send them, so there is no polling.
REST is used at startup (latest known positions) and once a day for the vessel
list, which goes straight into the SQLite vessel directory used by ship search.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Iterator

import aiohttp
import paho.mqtt.client as mqtt

from .config import USER_AGENT
from .fleets import FleetMatcher, normalize, vessel_length
from .store import Store
from .tracker import Tracker

_LOGGER = logging.getLogger(__name__)

HOST = "meri.digitraffic.fi"
REST = f"https://{HOST}/api/ais/v1"
HEADERS = {"Digitraffic-User": "ha-ferry-watch", "User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
_SKIP = " \t\r\n,"


def iter_json_array(text: str) -> Iterator[Any]:
    """The items of a JSON array one at a time. Digitraffic's vessel list parses into
    about 15 MB of Python objects at once; decoded item by item only one is alive."""
    decoder = json.JSONDecoder()
    i, n = text.index("[") + 1, len(text)
    while True:
        while i < n and text[i] in _SKIP:
            i += 1
        if i >= n or text[i] == "]":
            return
        item, i = decoder.raw_decode(text, i)
        yield item


def directory_row(v: dict[str, Any]) -> tuple | None:
    """A vessel-list entry as a row of the vessel directory, or None if unusable."""
    try:
        mmsi = int(v["mmsi"])
    except (KeyError, TypeError, ValueError):
        return None
    name = " ".join(str(v.get("name") or "").replace("@", " ").split())
    destination = " ".join(str(v.get("destination") or "").replace("@", " ").split()) or None
    ts = v.get("timestamp")
    ship_type = v.get("shipType")
    return (mmsi, name or None, normalize(name) or None, ship_type if isinstance(ship_type, int) else None,
            vessel_length(v) or None, destination, int(ts / 1000) if isinstance(ts, (int, float)) else None)


def display(name: str | None) -> str | None:
    return name.title() if name and name.isupper() else name


class DigitrafficFeed:
    def __init__(self, loop: asyncio.AbstractEventLoop, session: aiohttp.ClientSession,
                 store: Store, tracker: Tracker):
        self.loop = loop
        self.session = session
        self.store = store
        self.tracker = tracker  # the configured ships
        self.trackers = [tracker]  # plus the fleets, once added
        self.client: mqtt.Client | None = None
        self.subscribed: set[int] = set()
        self.connected = False
        self.last_message_ts: float | None = None
        self.message_count = 0
        self._directory_lock = asyncio.Lock()
        # Stands in for the vessel list download in simulation (FW_SIMULATE=1).
        self.vessel_list_source = None

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

    def add_tracker(self, tracker: Tracker) -> None:
        self.trackers.append(tracker)

    def _dispatch(self, mmsi: int, kind: str, data: dict[str, Any]) -> None:
        self.last_message_ts = time.time()
        self.message_count += 1
        for tracker in self.trackers:
            if mmsi not in tracker.ships:
                continue
            if kind.startswith("location"):
                tracker.on_location(mmsi, data)
            elif kind == "metadata":
                tracker.on_metadata(mmsi, data)

    def wanted(self) -> set[int]:
        return set().union(*(t.ships for t in self.trackers))

    def sync_subscriptions(self) -> None:
        """Match MQTT subscriptions to the ships the trackers follow."""
        if not self.client or not self.connected:
            return
        wanted = self.wanted()
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
        if mmsis is None:
            await self.resolve_reflagged()
        for mmsi in mmsis if mmsis is not None else list(self.wanted()):
            try:
                data = await self._get_json(f"{REST}/locations", {"mmsi": mmsi})
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Could not fetch latest position for %s: %s", mmsi, err)
                continue
            for feature in data.get("features", []):
                msg = rest_feature_to_location(feature)
                if msg:
                    self._dispatch_quiet(mmsi, "location", msg)
            try:
                meta = await self._get_json(f"{REST}/vessels/{mmsi}")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
            if isinstance(meta, dict) and meta:
                self._dispatch_quiet(mmsi, "metadata", meta)

    def _dispatch_quiet(self, mmsi: int, kind: str, data: dict[str, Any]) -> None:
        # REST data must not count as live messages (the feed watchdog relies on them).
        last, count = self.last_message_ts, self.message_count
        self._dispatch(mmsi, kind, data)
        self.last_message_ts, self.message_count = last, count

    async def _vessel_list_text(self) -> str:
        if self.vessel_list_source:
            return self.vessel_list_source()
        async with self.session.get(f"{REST}/vessels", headers=HEADERS,
                                    timeout=aiohttp.ClientTimeout(total=120)) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def refresh_directory(self, matcher: FleetMatcher | None = None) -> int:
        """Download Digitraffic's vessel list into the vessel directory, handing each vessel
        to `matcher` on the way. Returns the number of vessels. Raises on network errors, and
        ValueError if the answer isn't a JSON array (the old directory then stays)."""
        async with self._directory_lock:
            text = await self._vessel_list_text()

            def rows():
                for v in iter_json_array(text):
                    if not isinstance(v, dict):
                        continue
                    if matcher:
                        matcher.add(v)
                    row = directory_row(v)
                    if row:
                        yield row
            count = self.store.replace_vessels(rows())
            self.store.kv_set("vessels_ts", time.time())
            return count

    def search(self, query: str, limit: int = 15) -> list[dict[str, Any]]:
        """Ship search in the vessel directory: name contains the query, or MMSI starts with it."""
        rows = self.store.search_vessels(normalize(query.strip()), limit)
        return [{
            "mmsi": r["mmsi"],
            "name": display(r["name"]) or f"MMSI {r['mmsi']}",
            "destination": r["destination"],
            "ship_type": r["type"],
            "length": r["length"],
        } for r in rows]

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
        if not self.store.vessel_count():
            try:
                await self.refresh_directory()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return
        for ship in missing:
            matches = [m for m in self.store.vessels_named(normalize(ship["name"])) if m != ship["mmsi"]]
            if len(matches) == 1:
                new = matches[0]
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
