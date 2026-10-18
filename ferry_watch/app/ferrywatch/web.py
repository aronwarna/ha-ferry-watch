"""HTTP API and panel, served through Home Assistant ingress.

All URLs in the panel are relative, because ingress serves it under a
per-session path prefix.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from . import __version__, stats
from .charts import in_finnish_waters
from .config import DEV
from .fleets import FLEET_IDS
from .vchart import MIN_Z

TILE_WAIT = 0.3  # s a drawn-chart tile request may wait for its data before answering "pending"

if TYPE_CHECKING:
    from .__main__ import FerryWatch

_LOGGER = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
INGRESS_PROXY = "172.30.32.2"
TAIL_MAX_MINUTES = 24 * 60


@web.middleware
async def ingress_only(request: web.Request, handler):
    # Only the Supervisor's ingress proxy may reach the panel (it handles HA login).
    if not DEV and request.remote not in (INGRESS_PROXY, "127.0.0.1", "::1"):
        raise web.HTTPForbidden()
    return await handler(request)


ZONE_ROLES = {"inner": "Inner route", "outer": "Outer route", "phone": "My area"}


def _points(value: Any, minimum: int) -> list[list[float]]:
    try:
        points = [[float(lat), float(lon)] for lat, lon in value]
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Points must be [lat, lon] pairs") from None
    if len(points) < minimum or len(points) > 500:
        raise web.HTTPBadRequest(text=f"Needs at least {minimum} points")
    if not all(-90 <= lat <= 90 and -180 <= lon <= 180 for lat, lon in points):
        raise web.HTTPBadRequest(text="Point out of range")
    return points


def _mmsi(request: web.Request) -> int:
    try:
        return int(request.match_info["mmsi"])
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid MMSI") from None


class Web:
    def __init__(self, fw: FerryWatch):
        self.fw = fw
        self.sockets: weakref.WeakSet[web.WebSocketResponse] = weakref.WeakSet()
        self.app = web.Application(middlewares=[ingress_only], client_max_size=10 * 1024 * 1024)
        r = self.app.router
        r.add_get("/", self.index)
        r.add_get("/ship/{mmsi}", self.index)
        r.add_get("/api/state", self.state)
        r.add_get("/api/tails", self.tails)
        r.add_get("/api/track/{mmsi}", self.track)
        r.add_get("/api/photo/{mmsi}", self.photo)
        r.add_post("/api/ships/{mmsi}/photo", self.upload_photo)
        r.add_post("/api/ships", self.add_ship)
        r.add_delete("/api/ships/{mmsi}", self.remove_ship)
        r.add_post("/api/ships/{mmsi}/alerts", self.set_alerts)
        r.add_get("/api/search", self.search)
        r.add_get("/api/ws", self.websocket)
        r.add_get("/api/zones", self.zones)
        r.add_post("/api/zones", self.add_zone)
        r.add_put("/api/zones/{zone_id}", self.update_zone)
        r.add_delete("/api/zones/{zone_id}", self.delete_zone)
        r.add_get("/api/passages", self.passages)
        r.add_get("/api/passages.csv", self.passages_csv)
        r.add_get("/api/stats", self.stats)
        r.add_post("/api/notify/test", self.test_notification)
        r.add_post("/api/fleets/{fleet_id}", self.set_fleet)
        r.add_get("/api/tiles/{source}/{z}/{x}/{y}", self.tile)
        r.add_get("/api/vchart/{z}/{x}/{y}", self.vchart_tile)
        r.add_post("/api/aircraft", self.set_aircraft)
        r.add_post("/api/vchart/keep", self.set_vchart_keep)
        r.add_post("/api/follow", self.follow)
        r.add_delete("/api/follow/{mmsi}", self.unfollow)
        r.add_static("/static/", STATIC, append_version=False)

    # --- pages -----------------------------------------------------------

    async def index(self, request: web.Request) -> web.StreamResponse:
        html = (STATIC / "index.html").read_text()
        # A version on the panel's own files makes phones load the new ones after an update
        # instead of what they cached.
        for name in ("static/vchart.js", "static/app.js", "static/app.css"):
            html = html.replace(f'"{name}"', f'"{name}?v={__version__}"')
        # Ingress serves the panel under a per-session prefix; a <base> keeps relative
        # URLs working on deep links like /ship/<mmsi>.
        prefix = request.headers.get("X-Ingress-Path", "").rstrip("/")
        base = f"{prefix}/" if prefix or request.path != "/" else ""
        if base:
            html = html.replace("<head>", f'<head>\n  <base href="{base}">', 1)
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def state(self, request: web.Request) -> web.Response:
        return web.json_response(await self.fw.state())

    async def tails(self, request: web.Request) -> web.Response:
        minutes = min(int(request.query.get("minutes", 60)), TAIL_MAX_MINUTES)
        since = int(time.time()) - minutes * 60
        mmsis = [*self.fw.tracker.ships, *self.fw.fleet_tracker.ships]
        data = {str(m): self.fw.store.track(m, since) for m in mmsis}
        return web.json_response(data)

    async def track(self, request: web.Request) -> web.Response:
        mmsi = _mmsi(request)
        since_q = request.query.get("since", "voyage")
        try:
            until = int(request.query["until"]) if "until" in request.query else None
            since = None if since_q == "voyage" else int(since_q)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid time") from None
        if until is not None:
            # A stretch of history (passage replay); works for removed ships too.
            points = self.fw.store.track(mmsi, since or 0, until)
            return web.json_response({"mmsi": mmsi, "since": since, "until": until, "points": points})
        tracker = self.fw.tracker_for(mmsi)
        if not tracker:
            raise web.HTTPNotFound()
        if since is None:
            since = tracker.voyage_start(mmsi) or 0
        points = self.fw.store.track(mmsi, since, speed=True)
        last = tracker.ships[mmsi].last
        # The latest position may not have been stored yet (thinning); include it.
        if last and (not points or points[-1][2] < last.ts):
            points.append([last.lat, last.lon, last.ts, last.sog])
        return web.json_response({"mmsi": mmsi, "since": since, "points": points})

    async def tile(self, request: web.Request) -> web.StreamResponse:
        try:
            z, x = int(request.match_info["z"]), int(request.match_info["x"])
            y = int(request.match_info["y"].split(".")[0])
        except ValueError:
            raise web.HTTPBadRequest() from None
        source = request.match_info["source"]
        if source == "seamark-abroad":  # chart view: the Finnish charts draw their own marks
            if in_finnish_waters(z, x, y):
                raise web.HTTPNotFound(headers={"Cache-Control": "max-age=604800"})
            source = "seamark"
        url = self.fw.tile_url(source, z, x, y)
        if not url or not (0 <= z <= 20 and 0 <= x < 2**z and 0 <= y < 2**z):
            raise web.HTTPNotFound()
        path = await self.fw.tiles.get(source, z, x, y, url)
        if not path:
            raise web.HTTPNotFound(headers={"Cache-Control": "no-store"})  # may just be a failed fetch
        return web.FileResponse(path, headers={"Cache-Control": "max-age=604800", "Content-Type": "image/png"})

    async def vchart_tile(self, request: web.Request) -> web.Response:
        try:
            z, x = int(request.match_info["z"]), int(request.match_info["x"])
            y = int(request.match_info["y"].split(".")[0])
        except ValueError:
            raise web.HTTPBadRequest() from None
        if not (MIN_Z <= z <= 20 and 0 <= x < 2**z and 0 <= y < 2**z):
            raise web.HTTPNotFound()
        result = await self.fw.vchart.tile(z, x, y, wait=TILE_WAIT)
        if result is None:
            # Still downloading: answer now and let the panel ask again, so no
            # request sits open long enough to block the browser's other ones.
            return web.json_response({"pending": True}, status=202, headers={"Cache-Control": "no-store"})
        tile, complete = result
        # A tile missing a block that couldn't be fetched is asked for again next time.
        cache = "max-age=86400" if complete else "no-store"
        return web.Response(body=json.dumps(tile, separators=(",", ":")), content_type="application/json",
                            headers={"Cache-Control": cache})

    async def photo(self, request: web.Request) -> web.StreamResponse:
        path = self.fw.photos.path(_mmsi(request))
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})

    async def upload_photo(self, request: web.Request) -> web.Response:
        mmsi = _mmsi(request)
        if not self.fw.store.ship(mmsi) and mmsi not in self.fw.fleets.members:
            raise web.HTTPNotFound()
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "photo":
            raise web.HTTPBadRequest(text="Missing photo")
        body = await part.read(decode=False)
        try:
            self.fw.photos.save_upload(mmsi, bytes(body), part.headers.get("Content-Type", ""))
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from None
        await self.broadcast_ships()
        return web.json_response({"ok": True})

    @staticmethod
    async def _ship_body(request: web.Request) -> tuple[int, str]:
        body = await request.json()
        try:
            mmsi = int(body["mmsi"])
        except (KeyError, TypeError, ValueError):
            raise web.HTTPBadRequest(text="Invalid MMSI") from None
        if not 100_000_000 <= mmsi <= 999_999_999:
            raise web.HTTPBadRequest(text="An MMSI has 9 digits")
        return mmsi, str(body.get("name") or "").strip()[:60] or f"MMSI {mmsi}"

    async def add_ship(self, request: web.Request) -> web.Response:
        mmsi, name = await self._ship_body(request)
        if mmsi in self.fw.fleets.followed:
            await self.fw.unfollow(mmsi)
        await self.fw.add_ship(mmsi, name)
        return web.json_response({"ok": True})

    async def follow(self, request: web.Request) -> web.Response:
        mmsi, name = await self._ship_body(request)
        await self.fw.follow(mmsi, name)
        return web.json_response({"ok": True})

    async def unfollow(self, request: web.Request) -> web.Response:
        await self.fw.unfollow(_mmsi(request))
        return web.json_response({"ok": True})

    async def set_alerts(self, request: web.Request) -> web.Response:
        mmsi = _mmsi(request)
        if not self.fw.store.ship(mmsi):
            raise web.HTTPNotFound()
        body = await request.json()
        self.fw.store.set_alerts(mmsi, bool(body.get("enabled")))
        await self.broadcast_ships()
        return web.json_response({"ok": True})

    async def remove_ship(self, request: web.Request) -> web.Response:
        await self.fw.remove_ship(_mmsi(request))
        return web.json_response({"ok": True})

    async def search(self, request: web.Request) -> web.Response:
        return web.json_response(await self.fw.feed.search(request.query.get("q", "")))

    # --- zones and passages ---------------------------------------------

    async def zones(self, request: web.Request) -> web.Response:
        return web.json_response(self.fw.store.zones())

    async def add_zone(self, request: web.Request) -> web.Response:
        body = await request.json()
        role, shape = body.get("role"), body.get("shape")
        if role not in ZONE_ROLES or shape not in ("line", "polygon"):
            raise web.HTTPBadRequest(text="Unknown zone type")
        if role == "phone" and shape != "polygon":
            raise web.HTTPBadRequest(text="Your area must be a shape, not a line")
        points = _points(body.get("points"), 2 if shape == "line" else 3)
        name = str(body.get("name") or ZONE_ROLES[role]).strip()[:80]
        zone_id = self.fw.store.add_zone(name, role, shape, points)
        self.fw.zones_changed()
        return web.json_response(self.fw.store.zone(zone_id))

    async def update_zone(self, request: web.Request) -> web.Response:
        zone = self._zone_or_404(request)
        body = await request.json()
        points = _points(body.get("points", zone["points"]), 2 if zone["shape"] == "line" else 3)
        name = str(body.get("name") or zone["name"]).strip()[:80]
        self.fw.store.update_zone(zone["id"], name, points)
        self.fw.zones_changed()
        return web.json_response(self.fw.store.zone(zone["id"]))

    async def delete_zone(self, request: web.Request) -> web.Response:
        zone = self._zone_or_404(request)
        self.fw.store.delete_zone(zone["id"])
        self.fw.zones_changed()
        return web.json_response({"ok": True})

    def _zone_or_404(self, request: web.Request) -> dict[str, Any]:
        try:
            zone = self.fw.store.zone(int(request.match_info["zone_id"]))
        except ValueError:
            zone = None
        if not zone:
            raise web.HTTPNotFound()
        return zone

    async def passages(self, request: web.Request) -> web.Response:
        limit = min(int(request.query.get("limit", 30)), 500)
        names = stats.ship_names(self.fw.store)
        rows = self.fw.store.passages(limit=limit, route=request.query.get("route"))
        for row in rows:
            info = names.get(row["mmsi"], {})
            row["name"] = info.get("name", f"MMSI {row['mmsi']}")
            row["color"] = info.get("color", "#64748b")
        return web.json_response(rows)

    async def passages_csv(self, request: web.Request) -> web.Response:
        return web.Response(
            text=stats.to_csv(self.fw.store), content_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="ferry-watch-passages.csv"'},
        )

    async def stats(self, request: web.Request) -> web.Response:
        return web.json_response(stats.payload(self.fw.store))

    async def set_fleet(self, request: web.Request) -> web.Response:
        fleet_id = request.match_info["fleet_id"]
        if fleet_id not in FLEET_IDS:
            raise web.HTTPNotFound()
        body = await request.json()
        await self.fw.set_fleet(fleet_id, bool(body.get("enabled")))
        return web.json_response({"ok": True})

    async def set_aircraft(self, request: web.Request) -> web.Response:
        body = await request.json()
        await self.fw.set_aircraft(bool(body.get("enabled")))
        return web.json_response({"ok": True})

    async def set_vchart_keep(self, request: web.Request) -> web.Response:
        body = await request.json()
        await self.fw.set_vchart_keep(bool(body.get("enabled")))
        return web.json_response({"ok": True})

    async def test_notification(self, request: web.Request) -> web.Response:
        try:
            message = await self.fw.notifier.send_test()
        except RuntimeError as err:
            raise web.HTTPBadRequest(text=str(err)) from None
        except Exception as err:  # noqa: BLE001 - report any failure to the panel
            _LOGGER.exception("Test notification failed")
            raise web.HTTPBadGateway(text=f"Sending failed: {err}") from None
        return web.json_response({"message": message})

    # --- live updates ----------------------------------------------------

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.sockets.discard(ws)
        return ws

    def on_event(self, kind: str, payload: dict[str, Any]) -> None:
        if self.sockets:
            self._send({"type": kind, "ship": payload})

    def on_fleet_event(self, kind: str, payload: dict[str, Any]) -> None:
        if self.sockets:
            self._send({"type": kind, "ship": self.fw.fleet_summary(payload["mmsi"])})

    def broadcast_aircraft(self, aircraft: list[dict[str, Any]]) -> None:
        self._send({"type": "aircraft", "aircraft": aircraft})

    async def broadcast_ships(self) -> None:
        self._send({"type": "reload"})

    def _send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message)
        for ws in list(self.sockets):
            if not ws.closed:
                asyncio.ensure_future(self._safe_send(ws, data))

    @staticmethod
    async def _safe_send(ws: web.WebSocketResponse, data: str) -> None:
        try:
            await ws.send_str(data)
        except (ConnectionError, RuntimeError):
            pass
