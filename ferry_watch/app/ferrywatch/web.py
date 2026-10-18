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

from .config import DEV

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
        r.add_get("/api/search", self.search)
        r.add_get("/api/ws", self.websocket)
        r.add_get("/api/zones", self.zones)
        r.add_post("/api/zones", self.add_zone)
        r.add_put("/api/zones/{zone_id}", self.update_zone)
        r.add_delete("/api/zones/{zone_id}", self.delete_zone)
        r.add_get("/api/passages", self.passages)
        r.add_post("/api/notify/test", self.test_notification)
        r.add_static("/static/", STATIC, append_version=False)

    # --- pages -----------------------------------------------------------

    async def index(self, request: web.Request) -> web.StreamResponse:
        html = (STATIC / "index.html").read_text()
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
        data = {str(m): self.fw.store.track(m, since) for m in self.fw.tracker.ships}
        return web.json_response(data)

    async def track(self, request: web.Request) -> web.Response:
        mmsi = _mmsi(request)
        if mmsi not in self.fw.tracker.ships:
            raise web.HTTPNotFound()
        since_q = request.query.get("since", "voyage")
        if since_q == "voyage":
            since = self.fw.tracker.voyage_start(mmsi) or 0
        else:
            since = int(since_q)
        points = self.fw.store.track(mmsi, since)
        last = self.fw.tracker.ships[mmsi].last
        # The latest position may not have been stored yet (thinning); include it.
        if last and (not points or points[-1][2] < last.ts):
            points.append([last.lat, last.lon, last.ts])
        return web.json_response({"mmsi": mmsi, "since": since, "points": points})

    async def photo(self, request: web.Request) -> web.StreamResponse:
        path = self.fw.photos.path(_mmsi(request))
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})

    async def upload_photo(self, request: web.Request) -> web.Response:
        mmsi = _mmsi(request)
        if not self.fw.store.ship(mmsi):
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

    async def add_ship(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            mmsi = int(body["mmsi"])
        except (KeyError, TypeError, ValueError):
            raise web.HTTPBadRequest(text="Invalid MMSI") from None
        if not 100_000_000 <= mmsi <= 999_999_999:
            raise web.HTTPBadRequest(text="An MMSI has 9 digits")
        name = str(body.get("name") or "").strip() or f"MMSI {mmsi}"
        await self.fw.add_ship(mmsi, name)
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
        names = {s["mmsi"]: s["name"] for s in self.fw.store.ships()}
        rows = self.fw.store.passages(limit=limit, route=request.query.get("route"))
        for row in rows:
            row["name"] = names.get(row["mmsi"], f"MMSI {row['mmsi']}")
        return web.json_response(rows)

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
