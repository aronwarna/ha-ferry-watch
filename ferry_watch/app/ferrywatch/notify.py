"""Phone notifications: inner-route passages and feed problems."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiohttp

from . import config
from .eta import estimate
from .geo import distance_m, point_in_polygon
from .mapimage import http_tile_fetcher, render
from .passages import DIRECTION_LABELS

if TYPE_CHECKING:
    from .__main__ import FerryWatch

_LOGGER = logging.getLogger(__name__)

TRACK_MINUTES = 40  # track shown in the notification picture
PHONE_IN_PICTURE_M = 25_000
FEED_SILENCE = 30 * 60
FEED_WARN_FROM, FEED_WARN_UNTIL = 6, 21  # local hours
MAX_PASSAGE_AGE = 10 * 60  # don't alert about passages detected from old data
IMAGE_KEEP_DAYS = 14
LAST_INNER_ENTITY = "sensor.ferry_watch_last_inner_passage"


def feed_warning_hours(now: float | None = None) -> bool:
    """Feed warnings only go out by day; inner passages happen around 08-10 and 17-19."""
    return FEED_WARN_FROM <= time.localtime(now).tm_hour < FEED_WARN_UNTIL


def clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


class Notifier:
    def __init__(self, fw: FerryWatch):
        self.fw = fw
        self.panel_url: str | None = None
        self.feed_warned = False
        self._tasks: set[asyncio.Task] = set()

    # --- passages ---------------------------------------------------------

    def on_passage(self, passage: dict[str, Any]) -> None:
        task = asyncio.create_task(self.handle(passage))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle(self, passage: dict[str, Any]) -> None:
        notified, note = await self._alert(passage)
        self.fw.store.set_passage_result(passage["id"], notified, note)
        await self.publish_to_ha(passage, notified, note)

    async def _alert(self, passage: dict[str, Any]) -> tuple[bool, str]:
        """Send the phone alert if this passage calls for one; returns (sent, note)."""
        store = self.fw.store
        try:
            if passage["route"] != "inner":
                return False, "outer route"
            if not self.fw.ha:
                return False, "Home Assistant not connected"
            if time.time() - passage["ts"] > MAX_PASSAGE_AGE:
                return False, "detected late (old data)"
            opts = self.fw.options
            phone_zones = [z for z in store.zones() if z["role"] == "phone"]
            phone = await self.fw.ha.phone_location(opts.phone_entity, opts.notify_service,
                                                    opts.phone_max_age_minutes * 60)
            if opts.require_phone_area and phone_zones:
                if not phone:
                    return False, "phone location unknown"
                if not any(point_in_polygon(phone[0], phone[1], z["points"]) for z in phone_zones):
                    return False, "phone outside your area"
            await self.send_passage(passage, phone[:2] if phone else None)
            return True, "sent"
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("Notification for passage %s failed: %s", passage.get("id"), err)
            return False, f"sending failed: {err}"
        except Exception:  # noqa: BLE001 - never let a notification bug stop tracking
            _LOGGER.exception("Notification for passage %s failed", passage.get("id"))
            return False, "error, see log"

    async def publish_to_ha(self, passage: dict[str, Any], notified: bool, note: str) -> None:
        """Fire a ferry_watch_passage event, and keep the last-inner-passage sensor current,
        for anyone who wants their own automations."""
        if not self.fw.ha:
            return
        ship = self.fw.store.ship(passage["mmsi"])
        data = {
            "ship": ship["name"] if ship else f"MMSI {passage['mmsi']}",
            "mmsi": passage["mmsi"],
            "route": passage["route"],
            "direction": DIRECTION_LABELS.get(passage["direction"], passage["direction"]),
            "time": datetime.fromtimestamp(passage["ts"], timezone.utc).isoformat(),
            "speed_kn": passage.get("sog"),
            "latitude": passage.get("lat"),
            "longitude": passage.get("lon"),
            "alert_sent": notified,
            "alert_note": note,
        }
        try:
            await self.fw.ha.fire_event("ferry_watch_passage", data)
            if passage["route"] == "inner":
                await self.publish_last_inner(data)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Could not send the passage to Home Assistant: %s", err)

    async def publish_last_inner(self, data: dict[str, Any] | None = None) -> None:
        """sensor.ferry_watch_last_inner_passage; with no data, restore it from the database
        (states set through the API don't survive a Home Assistant restart)."""
        if data is None:
            rows = self.fw.store.passages(limit=1, route="inner")
            if not rows:
                return
            row = rows[0]
            ship = self.fw.store.ship(row["mmsi"])
            data = {
                "ship": ship["name"] if ship else f"MMSI {row['mmsi']}",
                "mmsi": row["mmsi"],
                "direction": DIRECTION_LABELS.get(row["direction"], row["direction"]),
                "time": datetime.fromtimestamp(row["ts"], timezone.utc).isoformat(),
                "speed_kn": row["sog"],
                "alert_sent": bool(row["notified"]),
            }
        attrs = {
            "friendly_name": "Ferry Watch last inner passage",
            "device_class": "timestamp",
            "icon": "mdi:ferry",
            **{k: data[k] for k in ("ship", "mmsi", "direction", "speed_kn", "alert_sent") if k in data},
        }
        await self.fw.ha.set_state(LAST_INNER_ENTITY, data["time"], attrs)

    def build_message(self, passage: dict[str, Any], phone: tuple[float, float] | None,
                      now: float | None = None) -> tuple[str, str]:
        now = now or time.time()
        ship = self.fw.store.ship(passage["mmsi"])
        name = ship["name"] if ship else f"MMSI {passage['mmsi']}"
        title = f"{name}: {passage['route']} route"
        label = DIRECTION_LABELS[passage["direction"]]
        parts = [label[0].upper() + label[1:]]
        if passage.get("sog"):
            parts[0] += f", {passage['sog']:.0f} kn"
        message = parts[0] + "."
        if phone:
            eta = estimate(self.fw.store, passage, (passage["lat"], passage["lon"]), passage.get("sog"), phone)
            if eta:
                minutes = max(1, round(eta["seconds"] / 60))
                message += f" Passes nearest you in about {minutes} min ({clock(now + eta['seconds'])})."
        return title, message

    async def send_passage(self, passage: dict[str, Any], phone: tuple[float, float] | None,
                           test: bool = False) -> None:
        title, message = self.build_message(passage, phone)
        if test:
            title = f"Test: {title}"
        data: dict[str, Any] = {
            "tag": f"ferry_watch_{passage['mmsi']}",
            "group": "ferry_watch",
            "channel": "Ferry Watch",
            "priority": "high",
            "ttl": 0,
        }
        url = await self.get_panel_url()
        if url:
            data["clickAction"] = url  # Android
            data["url"] = url  # iOS
        photo = self.fw.photos.remote_url(passage["mmsi"]) if self.fw.photos else None
        if photo:
            data["icon_url"] = photo
        image = await self.picture(passage, phone)
        if image:
            data["image"] = image
        await self.fw.ha.call_service(self.fw.options.notify_service,
                                      {"title": title, "message": message, "data": data})
        self.fw.store.kv_set("last_notification", {
            "passage_id": passage.get("id"), "mmsi": passage["mmsi"], "ts": time.time(),
        })
        _LOGGER.info("Notification sent: %s. %s", title, message)

    async def picture(self, passage: dict[str, Any], phone: tuple[float, float] | None) -> str | None:
        """Render the map picture into HA's media folder; returns its /media/local URL."""
        try:
            config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        mmsi = passage["mmsi"]
        state = self.fw.tracker.ships.get(mmsi)
        last = state.last if state else None
        ship = (last.lat, last.lon, last.heading if last.heading is not None else last.cog) if last \
            else (passage["lat"], passage["lon"], None)
        points = [(p[0], p[1]) for p in self.fw.store.track(mmsi, int(time.time()) - TRACK_MINUTES * 60)]
        extra = [phone] if phone and distance_m(*phone, *ship[:2]) <= PHONE_IN_PICTURE_M else None
        color = (self.fw.store.ship(mmsi) or {}).get("color", "#2563eb")
        try:
            png = await render(points, ship, color, http_tile_fetcher(self.fw.session), extra=extra)
        except Exception:  # noqa: BLE001 - a missing picture must not block the notification
            _LOGGER.exception("Could not draw the notification picture")
            return None
        name = f"passage-{passage.get('id') or int(time.time())}.png"
        (config.MEDIA_DIR / name).write_bytes(png)
        return f"/media/local/ferry_watch/{name}"

    def cleanup_pictures(self) -> None:
        cutoff = time.time() - IMAGE_KEEP_DAYS * 86400
        try:
            for path in config.MEDIA_DIR.glob("passage-*.png"):
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    async def get_panel_url(self) -> str | None:
        """Frontend URL of this app's panel, so tapping a notification opens it."""
        if self.panel_url or not config.SUPERVISOR_TOKEN:
            return self.panel_url
        try:
            async with self.fw.session.get(
                f"{config.SUPERVISOR_API}/addons/self/info",
                headers={"Authorization": f"Bearer {config.SUPERVISOR_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                info = (await resp.json())["data"]
            self.panel_url = f"/hassio/ingress/{info['slug']}"
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError) as err:
            _LOGGER.warning("Could not look up the panel URL: %s", err)
        return self.panel_url

    async def send_test(self) -> str:
        """Send a sample passage notification for the ship that moved most recently."""
        if not self.fw.ha:
            raise RuntimeError("Not running inside Home Assistant")
        states = [s for s in self.fw.tracker.ships.values() if s.last]
        if not states:
            raise RuntimeError("No ship positions yet")
        state = max(states, key=lambda s: ((s.last.sog or 0) > 1, s.last.ts))
        p = state.last
        passage = {
            "id": None, "mmsi": state.mmsi, "ts": p.ts, "route": "inner",
            "direction": "east" if p.cog is not None and 0 < p.cog < 180 else "west",
            "lat": p.lat, "lon": p.lon, "sog": p.sog,
        }
        opts = self.fw.options
        phone = await self.fw.ha.phone_location(opts.phone_entity, opts.notify_service,
                                                opts.phone_max_age_minutes * 60)
        await self.send_passage(passage, phone[:2] if phone else None, test=True)
        phone_zones = [z for z in self.fw.store.zones() if z["role"] == "phone"]
        if not phone:
            return "Sent. Your phone's location is unknown, so real alerts would be skipped."
        if phone_zones and not any(point_in_polygon(phone[0], phone[1], z["points"]) for z in phone_zones):
            return "Sent. Your phone is outside your area right now, so a real alert would be skipped."
        return "Sent. Your phone is in your area, so a real alert would go out."

    # --- feed watchdog ------------------------------------------------------

    async def watch_feed(self) -> None:
        started = time.time()
        while True:
            await asyncio.sleep(60)
            feed = self.fw.feed
            if not feed or not self.fw.ha or not self.fw.options.notify_feed_problems:
                continue
            last = feed.last_message_ts or started
            silent = time.time() - last > FEED_SILENCE
            try:
                if silent and not self.feed_warned and feed_warning_hours():
                    await self._feed_message(
                        "Ferry Watch: no ship data",
                        f"No ship positions since {clock(last)}. Passages can't be detected until it's back.")
                    self.feed_warned = True
                elif not silent and self.feed_warned:
                    await self._feed_message("Ferry Watch: ship data is back", "Live positions are arriving again.")
                    self.feed_warned = False
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Could not send feed notification: %s", err)

    async def _feed_message(self, title: str, message: str) -> None:
        data = {"tag": "ferry_watch_feed", "group": "ferry_watch", "channel": "Ferry Watch"}
        url = await self.get_panel_url()
        if url:
            data["clickAction"] = url
        await self.fw.ha.call_service(self.fw.options.notify_service,
                                      {"title": title, "message": message, "data": data})
