"""Turns raw AIS messages into stored, thinned tracks and voyages.

A voyage starts when a ship leaves port: it has been below STOP_SPEED for at
least PORT_STOP_SECONDS and then moves faster than MOVE_SPEED again. The map
shows each ship's track since the start of its current voyage.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .geo import angle_diff, nearest_port
from .store import Position, Store

_LOGGER = logging.getLogger(__name__)

STOP_SPEED = 1.0  # knots
MOVE_SPEED = 2.0  # knots
PORT_STOP_SECONDS = 20 * 60
STORE_INTERVAL_MOVING = 30  # seconds between stored points while under way
STORE_INTERVAL_STOPPED = 300  # seconds between stored points while stopped
TURN_DEGREES = 15  # store early when the course changes this much
MIN_TURN_INTERVAL = 5
# The crew-entered AIS ETA is shown from 2 days ahead until an hour after it has passed.
ETA_SHOWN_AHEAD = 2 * 86400
ETA_SHOWN_PAST = 3600

Listener = Callable[[str, dict[str, Any]], None]


@dataclass
class ShipState:
    mmsi: int
    last: Position | None = None
    last_stored: Position | None = None
    stopped_since: int | None = None
    in_port: bool = False
    port: str | None = None
    voyage_start: int | None = None
    voyage_from: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def parse_location(mmsi: int, data: dict[str, Any]) -> Position | None:
    """Parse a Digitraffic vessels-v2 location message. Returns None if unusable."""
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
        ts = int(data["time"])
    except (KeyError, TypeError, ValueError):
        return None
    # AIS uses 91/181 for "not available".
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    sog = data.get("sog")
    sog = float(sog) if isinstance(sog, (int, float)) and sog < 102.3 else None
    cog = data.get("cog")
    cog = float(cog) if isinstance(cog, (int, float)) and cog < 360 else None
    heading = data.get("heading")
    heading = int(heading) if isinstance(heading, (int, float)) and heading < 360 else None
    return Position(mmsi=mmsi, ts=ts, lat=lat, lon=lon, sog=sog, cog=cog, heading=heading)


def decode_eta(packed: Any, now: float | None = None) -> int | None:
    """AIS ETA (UTC month, day, hour and minute packed in bits 19-0, no year) as a
    timestamp, taking the year that puts it nearest to now; None when not given."""
    if not isinstance(packed, int) or packed <= 0:
        return None
    month, day = (packed >> 16) & 0xF, (packed >> 11) & 0x1F
    hour, minute = (packed >> 6) & 0x1F, packed & 0x3F
    if not (1 <= month <= 12 and day >= 1 and hour < 24 and minute < 60):
        return None
    now = time.time() if now is None else now
    year = datetime.fromtimestamp(now, timezone.utc).year
    best = None
    for y in (year - 1, year, year + 1):
        try:
            ts = datetime(y, month, day, hour, minute, tzinfo=timezone.utc).timestamp()
        except ValueError:  # 30 February and the like
            continue
        if best is None or abs(ts - now) < abs(best - now):
            best = ts
    return int(best) if best is not None else None


def parse_metadata(data: dict[str, Any]) -> dict[str, Any]:
    def clean(value: Any) -> Any:
        if isinstance(value, str):
            value = " ".join(value.replace("@", " ").split())
            return value or None
        return value

    ts = data.get("timestamp")
    return {
        "name": clean(data.get("name")),
        "destination": clean(data.get("destination")),
        "imo": data.get("imo") or None,
        "call_sign": clean(data.get("callSign")),
        "ship_type": data.get("type"),
        "eta_ts": decode_eta(data.get("eta")),
        # In tenths of a metre; 0 means not given.
        "draught": draught / 10 if isinstance((draught := data.get("draught")), (int, float)) and draught > 0 else None,
        # Metadata timestamps are milliseconds, location timestamps seconds.
        "updated_ts": int(ts / 1000) if isinstance(ts, (int, float)) else int(time.time()),
    }


class Tracker:
    """Follows a set of ships. The main tracker follows the configured ships; a second one
    follows the fleets, with its own member list and sparser storage."""

    def __init__(self, store: Store, members: Callable[[], set[int]] | None = None,
                 store_moving: int = STORE_INTERVAL_MOVING, store_stopped: int = STORE_INTERVAL_STOPPED):
        self.store = store
        self.members = members
        self.store_moving = store_moving
        self.store_stopped = store_stopped
        self.ships: dict[int, ShipState] = {}
        self.listeners: list[Listener] = []

    def load(self) -> None:
        """Rebuild in-memory state for this tracker's ships from the database."""
        wanted = self.members() if self.members else {s["mmsi"] for s in self.store.ships()}
        for mmsi in list(self.ships):
            if mmsi not in wanted:
                del self.ships[mmsi]
        for mmsi in wanted:
            if mmsi in self.ships:
                continue
            state = ShipState(mmsi=mmsi)
            state.last = state.last_stored = self.store.last_position(mmsi)
            if state.last and state.last.sog is not None and state.last.sog < STOP_SPEED:
                # Resume a stop that was in progress before a restart.
                state.stopped_since = self.store.stopped_since(mmsi, STOP_SPEED)
                if state.stopped_since and state.last.ts - state.stopped_since >= PORT_STOP_SECONDS:
                    state.in_port = True
                    state.port = nearest_port(state.last.lat, state.last.lon)
            voyage = self.store.current_voyage(mmsi)
            if voyage:
                state.voyage_start = voyage["start_ts"]
                state.voyage_from = voyage["from_port"]
            state.meta = self.store.meta(mmsi) or {}
            self.ships[mmsi] = state

    def subscribe(self, listener: Listener) -> None:
        self.listeners.append(listener)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        for listener in self.listeners:
            try:
                listener(kind, payload)
            except Exception:  # noqa: BLE001 - one bad listener must not stop tracking
                _LOGGER.exception("Listener failed")

    # --- incoming data ---------------------------------------------------

    def on_location(self, mmsi: int, data: dict[str, Any]) -> None:
        state = self.ships.get(mmsi)
        if state is None:
            return
        pos = parse_location(mmsi, data)
        if pos is None:
            return
        if state.last and pos.ts <= state.last.ts:
            return  # duplicate or out of order
        self._update_voyage(state, pos)
        state.last = pos
        if self._should_store(state, pos):
            self.store.insert_position(pos)
            state.last_stored = pos
        self._emit("position", self.ship_summary(mmsi))

    def on_metadata(self, mmsi: int, data: dict[str, Any]) -> None:
        state = self.ships.get(mmsi)
        if state is None:
            return
        meta = parse_metadata(data)
        state.meta = {**state.meta, **{k: v for k, v in meta.items() if v is not None}}
        self.store.upsert_meta(mmsi, state.meta)
        self._emit("meta", self.ship_summary(mmsi))

    def _update_voyage(self, state: ShipState, pos: Position) -> None:
        if pos.sog is None:
            return
        if pos.sog < STOP_SPEED:
            if state.stopped_since is None:
                state.stopped_since = pos.ts
            if not state.in_port and pos.ts - state.stopped_since >= PORT_STOP_SECONDS:
                state.in_port = True
                state.port = nearest_port(pos.lat, pos.lon)
                _LOGGER.info("%s: in port (%s)", state.mmsi, state.port or "unknown")
        elif pos.sog >= MOVE_SPEED:
            if state.in_port or state.voyage_start is None:
                start = pos.ts
                state.voyage_start = start
                state.voyage_from = state.port if state.in_port else None
                self.store.start_voyage(state.mmsi, start, state.voyage_from)
                _LOGGER.info("%s: new voyage from %s", state.mmsi, state.voyage_from or "unknown")
            state.in_port = False
            state.port = None
            state.stopped_since = None

    def _should_store(self, state: ShipState, pos: Position) -> bool:
        prev = state.last_stored
        if prev is None:
            return True
        elapsed = pos.ts - prev.ts
        stopped = pos.sog is not None and pos.sog < STOP_SPEED
        if elapsed >= (self.store_stopped if stopped else self.store_moving):
            return True
        if (
            not stopped
            and elapsed >= MIN_TURN_INTERVAL
            and pos.cog is not None
            and prev.cog is not None
            and angle_diff(pos.cog, prev.cog) >= TURN_DEGREES
        ):
            return True
        return False

    # --- queries ---------------------------------------------------------

    def voyage_start(self, mmsi: int) -> int | None:
        state = self.ships.get(mmsi)
        if state and state.voyage_start:
            return state.voyage_start
        return self.store.first_position_ts(mmsi)

    def ship_summary(self, mmsi: int) -> dict[str, Any]:
        state = self.ships[mmsi]
        last = state.last
        in_port, port = state.in_port, state.port
        if not in_port and last and last.sog is not None and last.sog < STOP_SPEED:
            # Moored ships may stop reporting before the stop is long enough to count;
            # stopped at a known port is in port.
            port = nearest_port(last.lat, last.lon)
            in_port = port is not None
        return {
            "mmsi": mmsi,
            "position": last.as_dict() if last else None,
            "in_port": in_port,
            "port": port,
            "stopped_since": state.stopped_since,
            "voyage_start": self.voyage_start(mmsi),
            "voyage_from": state.voyage_from,
            "destination": state.meta.get("destination"),
            "ais_name": state.meta.get("name"),
            "eta_ais": eta if (eta := state.meta.get("eta_ts")) and time.time() - ETA_SHOWN_PAST < eta < time.time() + ETA_SHOWN_AHEAD else None,
            "draught": state.meta.get("draught"),
        }
