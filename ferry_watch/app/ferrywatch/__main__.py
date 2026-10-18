"""Entry point: wires the feed, tracker, storage, HA publisher and web panel."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from typing import Any

import aiohttp
from aiohttp import web

from . import __version__
from .ais import DigitrafficFeed
from .charts import chart_layer
from .config import (DATA_DIR, DEFAULT_SHIPS, DEFAULT_ZONES, DEV, EXTRA_COLORS, PORT, SUPERVISOR_TOKEN,
                     Options, load_options)
from .ha import HAClient, HAPublisher
from .notify import Notifier
from .passages import Detector
from .photos import PhotoCache
from .store import Store
from .tracker import Tracker
from .web import Web

_LOGGER = logging.getLogger("ferrywatch")

HOUSEKEEPING_INTERVAL = 6 * 3600


class FerryWatch:
    def __init__(self, options: Options):
        self.options = options
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.store = Store(DATA_DIR / "ferrywatch.db")
        self._seed_ships()
        self._seed_zones()
        self.tracker = Tracker(self.store)
        self.tracker.load()
        self.notifier = Notifier(self)
        self.detector = Detector(self.store, self.notifier.on_passage)
        self.session: aiohttp.ClientSession | None = None
        self.feed: DigitrafficFeed | None = None
        self.photos: PhotoCache | None = None
        self.web = Web(self)
        self.ha: HAClient | None = None
        self.publisher: HAPublisher | None = None
        self.chart: dict[str, Any] | None = None
        self.tasks: list[asyncio.Task] = []

    def _seed_ships(self) -> None:
        if self.store.kv_get("seeded"):
            return
        for ship in DEFAULT_SHIPS:
            self.store.add_ship(ship["mmsi"], ship["name"], ship["color"], ship["wiki"], auto_resolve=True)
        self.store.kv_set("seeded", True)

    def _seed_zones(self) -> None:
        if self.store.kv_get("zones_seeded"):
            return
        for zone in DEFAULT_ZONES:
            self.store.add_zone(zone["name"], zone["role"], zone["shape"], zone["points"])
        self.store.kv_set("zones_seeded", True)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.session = aiohttp.ClientSession()
        self.photos = PhotoCache(DATA_DIR / "photos", self.session)
        self.feed = DigitrafficFeed(loop, self.session, self.store, self.tracker)
        self.tracker.subscribe(self.web.on_event)
        self.tracker.subscribe(self.detector.on_event)

        if os.environ.get("FW_FAKE_HA") == "1":
            from .simulate import FakeHA
            self.ha = FakeHA()
        elif SUPERVISOR_TOKEN and not DEV:
            self.ha = HAClient(self.session, SUPERVISOR_TOKEN)
            self.tasks.append(asyncio.create_task(self.notifier.watch_feed()))
            if self.options.ha_entities:
                self.publisher = HAPublisher(self.ha, self.options.ha_update_interval, self)
                self.tracker.subscribe(self.publisher.on_event)
                self.tasks.append(asyncio.create_task(self.publisher.run()))

        runner = web.AppRunner(self.web.app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        _LOGGER.info("Ferry Watch %s listening on port %s", __version__, PORT)

        if os.environ.get("FW_SIMULATE") == "1":
            from .simulate import run_simulation, seed_history
            if os.environ.get("FW_SIM_HISTORY") == "1":
                seed_history(self.store)
            self.tasks.append(asyncio.create_task(run_simulation(self.tracker)))
        else:
            self.tasks.append(asyncio.create_task(self.feed.start()))
            self.tasks.append(asyncio.create_task(self._load_chart()))
        self.tasks.append(asyncio.create_task(self._housekeeping()))

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.feed:
            self.feed.stop()
        if self.session:
            await self.session.close()
        self.store.close()

    async def _load_chart(self) -> None:
        self.chart = await chart_layer(self.session, self.store)
        if self.chart:
            await self.web.broadcast_ships()

    async def _housekeeping(self) -> None:
        while True:
            cutoff = int(time.time()) - self.options.retention_days * 86400
            removed = self.store.prune(cutoff)
            if removed:
                _LOGGER.info("Removed %s positions older than %s days", removed, self.options.retention_days)
            self.notifier.cleanup_pictures()
            for ship in self.store.ships():
                await self.photos.ensure(ship)
            await self.web.broadcast_ships()
            await asyncio.sleep(HOUSEKEEPING_INTERVAL)

    # --- used by the web API --------------------------------------------

    async def state(self) -> dict[str, Any]:
        ships = []
        for ship in self.store.ships():
            if ship["mmsi"] not in self.tracker.ships:
                continue
            ships.append({
                **self.tracker.ship_summary(ship["mmsi"]),
                "name": ship["name"],
                "color": ship["color"],
                "photo": self.photos.public_info(ship["mmsi"]) if self.photos else None,
            })
        feed = self.feed.status() if self.feed else {}
        return {
            "zones": self.store.zones(),
            "last_notification": self.store.kv_get("last_notification"),
            "ha_connected": self.ha is not None,
            "version": __version__,
            "now": time.time(),
            "ships": ships,
            "feed": feed,
            "simulated": os.environ.get("FW_SIMULATE") == "1",
            "chart": self.chart,
            "positions_stored": self.store.position_count(),
        }

    async def add_ship(self, mmsi: int, name: str) -> None:
        if self.store.ship(mmsi):
            return
        used = {s["color"] for s in self.store.ships()}
        color = next((c for c in EXTRA_COLORS if c not in used), EXTRA_COLORS[len(used) % len(EXTRA_COLORS)])
        self.store.add_ship(mmsi, name, color)
        self.tracker.load()
        self.feed.sync_subscriptions()
        await self.web.broadcast_ships()
        await self.feed.bootstrap([mmsi])
        asyncio.create_task(self._after_add(mmsi))

    async def _after_add(self, mmsi: int) -> None:
        ship = self.store.ship(mmsi)
        if ship:
            await self.photos.ensure(ship)
            await self.web.broadcast_ships()

    def zones_changed(self) -> None:
        self.detector.reload_zones()
        self._send_reload()

    def _send_reload(self) -> None:
        asyncio.create_task(self.web.broadcast_ships())

    async def remove_ship(self, mmsi: int) -> None:
        self.store.remove_ship(mmsi)
        self.tracker.load()
        self.feed.sync_subscriptions()
        await self.web.broadcast_ships()


async def main() -> None:
    options = load_options()
    logging.basicConfig(
        level=getattr(logging, str(options.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    fw = FerryWatch(options)
    await fw.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    _LOGGER.info("Stopping")
    await fw.stop()


if __name__ == "__main__":
    asyncio.run(main())
