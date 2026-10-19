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
from .aircraft import AircraftWatcher
from .charts import ENC_DEFAULT, chart_layers, enc_service, enc_tile_url
from .faults import Faults
from .fleets import FleetManager, FleetMatcher
from .config import (DATA_DIR, DEFAULT_SHIPS, DEFAULT_ZONES, DEV, EXTRA_COLORS, PORT, SUPERVISOR_TOKEN,
                     Options, load_options)
from .geo import sun_info
from .ha import HAClient, HAPublisher, NextPassageSensor
from .notify import Notifier, sun_place
from .passages import Detector
from .photos import PhotoCache
from .predict import Predictor
from .store import Store
from .tilecache import TileCache
from .tracker import Tracker
from .traffic import Traffic
from .vchart import VectorChart
from .weather import SeaWeather
from .web import Web

_LOGGER = logging.getLogger("ferrywatch")

HOUSEKEEPING_INTERVAL = 6 * 3600
HA_SENSOR_INTERVAL = 30 * 60
PANEL_TICK = 60  # s between sea-condition checks and phone reads while a panel is open
FLEET_KEEP = 3 * 86400  # fleet ships only need their current voyage
FLEET_REFRESH = 24 * 3600
SEAMARK_URL = "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png"
FLEET_STORE_MOVING, FLEET_STORE_STOPPED = 60, 600  # sparser tracks than the main ships


class FerryWatch:
    def __init__(self, options: Options):
        self.options = options
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.store = Store(DATA_DIR / "ferrywatch.db")
        self._seed_ships()
        self._seed_zones()
        self.tracker = Tracker(self.store)
        self.tracker.load()
        self.fleets = FleetManager(self.store)
        self.fleet_tracker = Tracker(self.store, members=self._fleet_members,
                                     store_moving=FLEET_STORE_MOVING, store_stopped=FLEET_STORE_STOPPED)
        self.fleet_tracker.load()
        self.notifier = Notifier(self)
        self.detector = Detector(self.store, self.notifier.on_passage)
        self.predictor = Predictor(self.store, self._next_passage_changed)
        self.next_sensor: NextPassageSensor | None = None
        self._background: set[asyncio.Task] = set()
        self.session: aiohttp.ClientSession | None = None
        self.feed: DigitrafficFeed | None = None
        self.photos: PhotoCache | None = None
        self.web = Web(self)
        self.ha: HAClient | None = None
        self.publisher: HAPublisher | None = None
        self.chart: dict[str, Any] | None = None
        self.enc: dict[str, str] = dict(ENC_DEFAULT)
        self.aircraft: AircraftWatcher | None = None
        self.tiles: TileCache | None = None
        self.vchart: VectorChart | None = None
        self.aircraft_enabled: bool = self.store.kv_get("aircraft_enabled", False)
        self.weather: SeaWeather | None = None
        self.weather_enabled: bool = self.store.kv_get("weather_enabled", True)
        self.phone: dict[str, Any] | None = None  # the phone's last position, read while a panel is open
        self._wakers: list[asyncio.Event] = []
        self.vchart_keep: bool = self.store.kv_get("vchart_keep", True)
        self._keep_task: asyncio.Task | None = None
        self.tasks: list[asyncio.Task] = []

    def _fleet_members(self) -> set[int]:
        return set(self.fleets.active(set(self.tracker.ships)))

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
        self.tiles = TileCache(DATA_DIR / "tiles", self.session)
        fake_chart = fake_fmi = fake_faults = fake_traffic = None
        if os.environ.get("FW_SIMULATE") == "1":
            from .simulate import fake_chart_layer as fake_chart
            from .simulate import fake_fmi_fetch as fake_fmi
            from .simulate import fake_faults_fetch as fake_faults
            from .simulate import fake_traffic_fetch as fake_traffic
        self.weather = SeaWeather(self.session, fake_fmi)
        # Both only answer the panel's requests: nothing runs while no one looks.
        self.faults = Faults(self.session, fake_faults)
        self.traffic = Traffic(self.session, self.store,
                               lambda: {*self.tracker.ships, *self.fleet_tracker.ships}, fake_traffic)
        mml_key = "simulated" if fake_chart else self.store.kv_get("mml_api_key", "")
        self.vchart = VectorChart(DATA_DIR / "vchart", self.session, fake_chart, mml_key)
        self.feed = DigitrafficFeed(loop, self.session, self.store, self.tracker)
        self.feed.add_tracker(self.fleet_tracker)
        self.tracker.subscribe(self.predictor.on_event)  # before the panel, which shows its result
        self.tracker.subscribe(self.web.on_event)
        self.tracker.subscribe(self.detector.on_event)
        self.fleet_tracker.subscribe(self.web.on_fleet_event)
        # Aircraft are only fetched while the switch is on and someone has the panel open.
        self.aircraft = AircraftWatcher(self.session, lambda: self.aircraft_enabled and bool(self.web.sockets),
                                        self.web.broadcast_aircraft)

        if os.environ.get("FW_FAKE_HA") == "1":
            from .simulate import FakeHA
            self.ha = FakeHA()
            self.next_sensor = NextPassageSensor(self.ha)
        elif SUPERVISOR_TOKEN and not DEV:
            self.ha = HAClient(self.session, SUPERVISOR_TOKEN)
            self.tasks.append(asyncio.create_task(self.notifier.watch_feed()))
            self.tasks.append(asyncio.create_task(self._keep_ha_sensor()))
            if self.options.ha_entities:
                self.next_sensor = NextPassageSensor(self.ha)
                self.publisher = HAPublisher(self.ha, self.options.ha_update_interval, self)
                self.tracker.subscribe(self.publisher.on_event)
                self.tasks.append(asyncio.create_task(self.publisher.run()))

        runner = web.AppRunner(self.web.app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        _LOGGER.info("Ferry Watch %s listening on port %s", __version__, PORT)

        if os.environ.get("FW_SIMULATE") == "1":
            from .simulate import FLEET_MEMBERS, fake_vessel_list, run_simulation, seed_history, simulate_aircraft
            if os.environ.get("FW_SIM_HISTORY") == "1":
                seed_history(self.store)
            self.feed.vessel_list_source = fake_vessel_list
            await self.feed.refresh_directory()
            self.fleets.update(FLEET_MEMBERS)
            self.fleet_tracker.load()
            self.tasks.append(asyncio.create_task(run_simulation(self.tracker, self.fleet_tracker)))
            self.tasks.append(asyncio.create_task(simulate_aircraft(self.aircraft)))
        else:
            self.tasks.append(asyncio.create_task(self.aircraft.run()))
            self.tasks.append(asyncio.create_task(self.feed.start()))
            self.tasks.append(asyncio.create_task(self._load_chart()))
            self.tasks.append(asyncio.create_task(self._refresh_fleets_daily()))
        self.tasks.append(asyncio.create_task(self._housekeeping()))
        self.tasks.append(asyncio.create_task(self._while_panel_open(self._weather_tick)))
        self.tasks.append(asyncio.create_task(self._while_panel_open(self._phone_tick)))
        if self.vchart_keep:
            self._start_keeping()

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.feed:
            self.feed.stop()
        if self.session:
            await self.session.close()
        self.store.close()

    async def _load_chart(self) -> None:
        self.enc = await enc_service(self.session, self.store)
        self.chart = await chart_layers(self.session, self.store)
        if self.chart:
            await self.web.broadcast_ships()

    async def _refresh_fleets_daily(self) -> None:
        while True:
            if time.time() - self.fleets.updated_ts >= FLEET_REFRESH or not self.store.vessel_count():
                await self.refresh_fleets()
            await asyncio.sleep(3600)

    async def refresh_fleets(self) -> None:
        """Work out fleet membership from Digitraffic's vessel list, which also refreshes
        the vessel directory used by ship search."""
        matcher = FleetMatcher()
        try:
            count = await self.feed.refresh_directory(matcher)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.warning("Could not update the fleets: %s", err)
            return
        _LOGGER.info("Vessel directory: %s vessels", count)
        members = matcher.result()
        if self.fleets.update(members):
            _LOGGER.info("Fleets: %s ships", len(members))
            await self.fleets_changed()

    async def fleets_changed(self) -> None:
        before = set(self.fleet_tracker.ships)
        self.fleet_tracker.load()
        self.feed.sync_subscriptions()
        await self.web.broadcast_ships()
        new = sorted(set(self.fleet_tracker.ships) - before)
        if new and not os.environ.get("FW_SIMULATE"):
            await self.feed.bootstrap(new)
            await self.web.broadcast_ships()
        if new:
            asyncio.create_task(self._fleet_photos())

    async def _fleet_photos(self) -> None:
        """Look up photos for followed fleet ships, one at a time (each is looked up once)."""
        changed = False
        for mmsi, info in self.fleets.active(set(self.tracker.ships)).items():
            if not self.photos.path(mmsi):
                changed |= await self.photos.ensure({"mmsi": mmsi, "name": info["name"]})
        if changed:
            await self.web.broadcast_ships()

    def _start_keeping(self) -> None:
        if self._keep_task is None or self._keep_task.done():
            self._keep_task = asyncio.create_task(self.vchart.keep())
            self.tasks.append(self._keep_task)

    async def set_vchart_keep(self, on: bool) -> None:
        """Keep the drawn chart of the whole Gulf of Finland on disk, or only what's been looked at."""
        self.vchart_keep = on
        self.store.kv_set("vchart_keep", on)
        if on:
            self._start_keeping()
        elif self._keep_task:
            self._keep_task.cancel()
            self._keep_task = None

    def set_mml_key(self, key: str) -> None:
        self.store.kv_set("mml_api_key", key.strip())
        self.vchart.set_mml_key(key)
        if self._keep_task:  # start a new pass now, to fetch the new part everywhere
            self._keep_task.cancel()
            self._keep_task = None
            self._start_keeping()

    # --- while a panel is open: sea conditions and the phone's position ----------------

    def panel_opened(self) -> None:
        """A panel connected: check the sea conditions and the phone now, not in a minute."""
        for wake in self._wakers:
            wake.set()

    async def _while_panel_open(self, tick) -> None:
        wake = asyncio.Event()
        self._wakers.append(wake)
        while True:
            if self.web.sockets:
                try:
                    await tick()
                except Exception:  # noqa: BLE001 - keep the loop alive
                    _LOGGER.exception("Panel update failed")
            try:
                await asyncio.wait_for(wake.wait(), PANEL_TICK)
            except asyncio.TimeoutError:
                pass
            wake.clear()

    async def _weather_tick(self) -> None:
        if not self.weather_enabled:
            return
        before = self.weather.fetched
        await self.weather.latest()
        if self.weather.fetched != before:
            self.web.broadcast({"type": "weather", "weather": self.weather_state()})

    async def _phone_tick(self) -> None:
        if not self.ha or not self.options.phone_entity:
            return
        fix = await self.ha.phone_state(self.options.phone_entity)
        phone = {**fix, "max_age": self.options.phone_max_age_minutes * 60} if fix else None
        if phone != self.phone:
            self.phone = phone
            self.web.broadcast({"type": "phone", "phone": phone})

    def weather_state(self) -> dict[str, Any]:
        cond = self.weather.conditions() if self.weather and self.weather_enabled else {}
        return {**cond, "enabled": self.weather_enabled, "sun": sun_info(*sun_place(self.store))}

    async def set_weather(self, on: bool) -> None:
        self.weather_enabled = on
        self.store.kv_set("weather_enabled", on)
        if on:
            self.panel_opened()
        self.web.broadcast({"type": "weather", "weather": self.weather_state()})

    async def set_aircraft(self, on: bool) -> None:
        self.aircraft_enabled = on
        self.store.kv_set("aircraft_enabled", on)
        if not on:
            self.aircraft.aircraft.clear()
        self.web.broadcast_aircraft(self.aircraft.snapshot())
        await self.web.broadcast_ships()

    async def follow(self, mmsi: int, name: str) -> None:
        self.fleets.follow(mmsi, name)
        await self.fleets_changed()

    async def unfollow(self, mmsi: int) -> None:
        self.fleets.unfollow(mmsi)
        await self.fleets_changed()

    async def set_fleet(self, fleet_id: str, on: bool) -> None:
        self.fleets.set_enabled(fleet_id, on)
        await self.fleets_changed()

    async def _keep_ha_sensor(self) -> None:
        # States set through the API are lost when Home Assistant restarts; re-send now and then.
        while True:
            try:
                await self.notifier.publish_last_inner()
                await self._publish_next_passage(force=True)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.debug("Could not update the passage sensors: %s", err)
            await asyncio.sleep(HA_SENSOR_INTERVAL)

    def spawn(self, coro) -> asyncio.Task:
        """Run a coroutine in the background, keeping a reference until it finishes."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _next_passage_changed(self) -> None:
        if self.next_sensor:
            self.spawn(self._publish_next_passage())

    async def _publish_next_passage(self, force: bool = False) -> None:
        """sensor.ferry_watch_next_passage, for the ships with alerts on."""
        if not self.next_sensor:
            return
        alerting = {s["mmsi"]: s["name"] for s in self.store.ships() if self.store.alerts_on(s["mmsi"])}
        best = self.predictor.soonest(set(alerting))
        mmsi, prediction = best if best else (None, None)
        try:
            await self.next_sensor.update(alerting.get(mmsi), mmsi, prediction, force=force)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Could not update the next passage sensor: %s", err)

    async def _housekeeping(self) -> None:
        while True:
            cutoff = int(time.time()) - self.options.retention_days * 86400
            removed = self.store.prune(cutoff)
            if removed:
                _LOGGER.info("Removed %s positions older than %s days", removed, self.options.retention_days)
            self.store.prune_untracked(int(time.time()) - FLEET_KEEP)
            self.notifier.cleanup_pictures()
            await asyncio.to_thread(self.tiles.prune)
            await asyncio.to_thread(self.vchart.prune)
            for ship in self.store.ships():
                await self.photos.ensure(ship)
            await self._fleet_photos()
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
                "next_passage": self.predictor.get(ship["mmsi"]),
                "alerts": self.store.alerts_on(ship["mmsi"]),
                "photo": self.photos.public_info(ship["mmsi"]) if self.photos else None,
            })
        fleet_ships = []
        for mmsi, info in self.fleets.active(set(self.tracker.ships)).items():
            if mmsi in self.fleet_tracker.ships:
                fleet_ships.append(self.fleet_summary(mmsi, info))
        feed = self.feed.status() if self.feed else {}
        return {
            "fleets": self.fleets.summary(),
            "fleet_ships": fleet_ships,
            "zones": self.store.zones(),
            "last_notification": self.store.kv_get("last_notification"),
            "ha_connected": self.ha is not None,
            "version": __version__,
            "now": time.time(),
            "ships": ships,
            "feed": feed,
            "simulated": os.environ.get("FW_SIMULATE") == "1",
            "chart": self.chart,
            "vchart": self.vchart.status() if self.vchart else None,
            "positions_stored": self.store.position_count(),
            "retention_days": self.options.retention_days,
            "weather": self.weather_state(),
            "phone": self.phone,
            "memory_mb": memory_mb(),
            "aircraft": {
                "enabled": self.aircraft_enabled,
                "error": self.aircraft.error if self.aircraft else None,
                "list": self.aircraft.snapshot() if self.aircraft and self.aircraft_enabled else [],
            },
        }

    def tile_url(self, source: str, z: int, x: int, y: int) -> str | None:
        """Upstream address of a chart or sea mark tile the panel asks for."""
        if source == "seamark":
            return SEAMARK_URL.format(z=z, x=x, y=y)
        if source == "enc":  # the panel asks for these as 512-pixel tiles one zoom out
            return enc_tile_url(self.enc, z, x, y) if 5 <= z <= 18 else None
        layer = next((l for l in (self.chart or {}).get("layers", []) if f"chart-{l['id']}" == source), None)
        if layer:
            ids = layer["matrix_ids"]
            if not layer["min_zoom"] <= z < len(ids) or not ids[z]:
                return None
            return (layer["url"].replace("{TileMatrix}", ids[z])
                    .replace("{x}", str(x)).replace("{y}", str(y)))
        return None

    def fleet_summary(self, mmsi: int, info: dict[str, Any] | None = None) -> dict[str, Any]:
        info = info or self.fleets.members.get(mmsi, {})
        return {
            **self.fleet_tracker.ship_summary(mmsi),
            "name": info.get("name", f"MMSI {mmsi}"),
            "fleet": info.get("fleet"),
            "color": self.fleets.color(info.get("fleet")),
            "photo": self.photos.public_info(mmsi) if self.photos else None,
        }

    def tracker_for(self, mmsi: int) -> Tracker | None:
        for tracker in (self.tracker, self.fleet_tracker):
            if mmsi in tracker.ships:
                return tracker
        return None

    async def add_ship(self, mmsi: int, name: str) -> None:
        if self.store.ship(mmsi):
            return
        used = {s["color"] for s in self.store.ships()}
        color = next((c for c in EXTRA_COLORS if c not in used), EXTRA_COLORS[len(used) % len(EXTRA_COLORS)])
        self.store.add_ship(mmsi, name, color)
        self.store.set_alerts(mmsi, False)  # only the four ferries alert unless switched on
        self.tracker.load()
        self.fleet_tracker.load()
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
        self.fleet_tracker.load()
        self.feed.sync_subscriptions()
        await self.web.broadcast_ships()


def memory_mb() -> float | None:
    """This process's resident memory (VmRSS), for the panel's settings."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError):
        pass
    return None


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
