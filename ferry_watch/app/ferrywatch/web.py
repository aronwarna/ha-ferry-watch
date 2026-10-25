"""HTTP API and panel, served through Home Assistant ingress.

All URLs in the panel are relative, because ingress serves it under a
per-session path prefix.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import re
import time
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from . import __version__, config, stats
from .airhistory import PERIODS
from .charts import in_finnish_waters
from .config import DEV
from .fleets import FLEET_IDS
from .vchart import MIN_Z

SAMPLES_KEPT = 10  # chart samples kept in the media folder (newest)
SAMPLE_NAME = re.compile(r"chart-[0-9.-]+N-[0-9.-]+E-[0-9-]+\.json")
TILE_WAIT = 0.3  # s a drawn-chart tile request may wait for its data before answering "pending"

if TYPE_CHECKING:
    from .__main__ import FerryWatch

_LOGGER = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
INGRESS_PROXY = "172.30.32.2"
TAIL_MAX_MINUTES = 24 * 60
FLEET_PUSH_SECONDS = 5


@web.middleware
async def ingress_only(request: web.Request, handler):
    # Only the Supervisor's ingress proxy may reach the panel (it handles HA login).
    if not DEV and request.remote not in (INGRESS_PROXY, "127.0.0.1", "::1"):
        raise web.HTTPForbidden()
    return await handler(request)


ZONE_ROLES = {"inner": "Inner route", "outer": "Outer route", "phone": "My area"}
MAX_ROUTES = 100
MAX_ROUTE_POINTS = 200


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
        self._fleet_pushed: dict[int, float] = {}  # mmsi -> when its last position went out
        self._fleet_pending: set[int] = set()
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
        r.add_get("/api/overlays", self.overlays)
        r.add_get("/api/overlay/{overlay_id}/{ts}", self.overlay_points)
        r.add_get("/api/overlay/{overlay_id}/{ts}/{z}/{x}/{y}", self.overlay_tile)
        r.add_post("/api/vchart/sample", self.vchart_sample)
        r.add_get("/api/vchart/sample/{name}", self.vchart_sample_file)
        r.add_post("/api/aircraft", self.set_aircraft)
        r.add_get("/api/aircraft/flights", self.air_flights)
        r.add_get("/api/aircraft/flights/{flight_id}", self.air_flight)
        r.add_get("/api/aircraft/overview", self.air_overview)
        r.add_post("/api/weather", self.set_weather)
        r.add_get("/api/faults", self.faults)
        r.add_get("/api/traffic", self.traffic)
        r.add_post("/api/vchart/keep", self.set_vchart_keep)
        r.add_post("/api/vchart/foreign", self.set_vchart_foreign)
        r.add_post("/api/vchart/key", self.set_mml_key)
        r.add_post("/api/follow", self.follow)
        r.add_delete("/api/follow/{mmsi}", self.unfollow)
        r.add_get("/api/routes", self.routes)
        r.add_post("/api/routes", self.save_route)
        r.add_delete("/api/routes/{route_id}", self.delete_route)
        r.add_get("/api/autoroute", self.autoroute)
        r.add_get("/api/stations", self.stations)
        r.add_get("/api/stations/{fmisid}/history", self.station_history)
        r.add_get("/api/harbours", self.harbours)
        r.add_static("/static/", STATIC, append_version=False)
        if DEV:
            # Every chart symbol drawn by vchart.js, for checking against Traficom's key.
            r.add_get("/dev/symbols", self.dev_symbols)

    # --- pages -----------------------------------------------------------

    async def dev_symbols(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(Path(__file__).parent / "devpages" / "symbols.html")

    async def index(self, request: web.Request) -> web.StreamResponse:
        html = (STATIC / "index.html").read_text()
        # A version on the panel's own files makes phones load the new ones after an update
        # instead of what they cached.
        for name in ("static/vchart.js", "static/navmath.js", "static/app.js", "static/app.css",
                     "static/extra.css", "static/playback.js", "static/history.js", "static/weather-stats.js",
                     "static/overlays.js"):
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
            points = self.fw.store.track(mmsi, since or 0, until, speed=True)
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

    async def overlays(self, request: web.Request) -> web.Response:
        on = {i for i in request.query.get("times", "").split(",") if i}
        return web.json_response({"overlays": await self.fw.overlays.listing(on), "now": time.time()},
                                 headers={"Cache-Control": "no-store"})

    async def overlay_tile(self, request: web.Request) -> web.StreamResponse:
        try:
            ts, z, x = (int(request.match_info[k]) for k in ("ts", "z", "x"))
            y = int(request.match_info["y"].split(".")[0])
        except ValueError:
            raise web.HTTPBadRequest() from None
        overlay = self.fw.overlays.registry.get(request.match_info["overlay_id"])
        path = await self.fw.overlays.tile(overlay.id, ts, z, x, y) if overlay else None
        if not path:
            raise web.HTTPNotFound(headers={"Cache-Control": "no-store"})  # may just be a failed fetch
        # A frame's picture doesn't change: the browser may keep it as long as the NAS does.
        age = int(min(overlay.max_age, 7 * 86400))
        return web.FileResponse(path, headers={"Cache-Control": f"max-age={age}", "Content-Type": "image/png"})

    async def overlay_points(self, request: web.Request) -> web.Response:
        try:
            ts = int(request.match_info["ts"])
        except ValueError:
            raise web.HTTPBadRequest() from None
        data = await self.fw.overlays.points(request.match_info["overlay_id"], ts)
        if data is None:
            raise web.HTTPNotFound(headers={"Cache-Control": "no-store"})
        return web.json_response(data, dumps=lambda d: json.dumps(d, separators=(",", ":")),
                                 headers={"Cache-Control": "max-age=600"})

    async def vchart_tile(self, request: web.Request) -> web.Response:
        try:
            z, x = int(request.match_info["z"]), int(request.match_info["x"])
            y = int(request.match_info["y"].split(".")[0])
        except ValueError:
            raise web.HTTPBadRequest() from None
        if not (MIN_Z <= z <= 20 and 0 <= x < 2**z and 0 <= y < 2**z):
            raise web.HTTPNotFound()
        result = await self.fw.vchart.tile_gz(z, x, y, wait=TILE_WAIT)
        if result is None:
            # Still downloading: answer now and let the panel ask again, so no
            # request sits open long enough to block the browser's other ones.
            return web.json_response({"pending": True}, status=202, headers={"Cache-Control": "no-store"})
        body, complete = result
        # A tile missing a block that couldn't be fetched is asked for again next time.
        headers = {"Cache-Control": "max-age=86400" if complete else "no-store", "Vary": "Accept-Encoding"}
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            headers["Content-Encoding"] = "gzip"  # tiles are kept gzipped: sent as they are
        else:
            body = await asyncio.to_thread(gzip.decompress, body)
        return web.Response(body=body, content_type="application/json", headers=headers)

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
        return web.json_response(self.fw.feed.search(request.query.get("q", "")[:60]))

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
        try:
            since = int(request.query["since"]) if "since" in request.query else None
            until = int(request.query["until"]) if "until" in request.query else None
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid time") from None
        rows = self.fw.store.passages(limit=limit, route=request.query.get("route"), since_ts=since,
                                      before_ts=until)
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

    # --- aircraft history ------------------------------------------------------

    @staticmethod
    def _period(request: web.Request) -> tuple[int, int]:
        now = int(time.time())
        try:
            if "period" in request.query:
                return now - PERIODS[request.query["period"]], now
            until = int(request.query.get("until", now))
            return int(request.query.get("since", until - 86400)), until
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="period is 24h, 7d, 30d or year; or since and until") from None

    async def air_flights(self, request: web.Request) -> web.Response:
        since, until = self._period(request)
        flights = self.fw.air_history.flights(since, until, request.query.get("icao"))
        return web.json_response({"since": since, "until": until, "flights": flights})

    async def air_flight(self, request: web.Request) -> web.Response:
        try:
            flight = self.fw.air_history.flight(int(request.match_info["flight_id"]))
        except ValueError:
            flight = None
        if not flight:
            raise web.HTTPNotFound()
        return web.json_response(flight)

    async def air_overview(self, request: web.Request) -> web.StreamResponse:
        """Every flight of a period as simplified lines. Written out a flight at a time,
        so even a year never sits in memory whole."""
        since, until = self._period(request)
        resp = web.StreamResponse(headers={"Content-Type": "application/json", "Cache-Control": "no-store"})
        resp.enable_compression()
        await resp.prepare(request)
        buf: list[str] = []
        size = 0
        for piece in self.fw.air_history.overview(since, until):
            buf.append(piece)
            size += len(piece)
            if size > 32768:
                await resp.write("".join(buf).encode())
                buf, size = [], 0
        await resp.write("".join(buf).encode())
        await resp.write_eof()
        return resp

    async def set_weather(self, request: web.Request) -> web.Response:
        body = await request.json()
        await self.fw.set_weather(bool(body.get("enabled")))
        return web.json_response({"ok": True})

    async def faults(self, request: web.Request) -> web.Response:
        return web.json_response({"faults": await self.fw.faults.get(), "error": self.fw.faults.error})

    async def traffic(self, request: web.Request) -> web.Response:
        try:
            lat, lon = float(request.query["lat"]), float(request.query["lon"])
            radius = float(request.query.get("r", 30))
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="lat, lon and r are needed")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or radius != radius:
            raise web.HTTPBadRequest(text="lat, lon and r are needed")
        ships = await self.fw.traffic.around(lat, lon, radius)
        return web.json_response({"ships": ships, "error": self.fw.traffic.error})

    async def vchart_sample(self, request: web.Request) -> web.Response:
        """Saves what the drawn chart has at a spot to the media folder (see VectorChart.sample),
        for sending when something there is drawn wrong."""
        body = await request.json()
        try:
            lat, lon = float(body["lat"]), float(body["lon"])
        except (KeyError, TypeError, ValueError):
            raise web.HTTPBadRequest(text="lat and lon are needed") from None
        if not (-85 <= lat <= 85 and -180 <= lon <= 180):
            raise web.HTTPBadRequest(text="lat and lon are needed")
        sample = await self.fw.vchart.sample(lon, lat)
        name = f"chart-{lat:.3f}N-{lon:.3f}E-{time.strftime('%Y%m%d-%H%M%S')}.json"
        path = config.MEDIA_DIR / "chart-samples" / name
        data = json.dumps(sample, separators=(",", ":"), ensure_ascii=False).encode()

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            for old in sorted(path.parent.glob("chart-*.json"), key=lambda p: p.stat().st_mtime)[:-SAMPLES_KEPT]:
                old.unlink(missing_ok=True)

        try:
            await asyncio.to_thread(write)
        except OSError as err:
            return web.json_response({"ok": False, "error": str(err)})
        return web.json_response({"ok": True, "file": name, "mb": round(len(data) / 1e6, 1),
                                  "tiles": list(sample["tiles"])})

    async def vchart_sample_file(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        path = config.MEDIA_DIR / "chart-samples" / name
        if not SAMPLE_NAME.fullmatch(name) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "application/json", "Cache-Control": "no-store",
                                               "Content-Disposition": f'attachment; filename="{name}"'})

    async def set_vchart_keep(self, request: web.Request) -> web.Response:
        body = await request.json()
        await self.fw.set_vchart_keep(bool(body.get("enabled")))
        return web.json_response({"ok": True})

    async def set_vchart_foreign(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.fw.set_vchart_foreign(bool(body.get("enabled")))
        return web.json_response({"ok": True})

    async def set_mml_key(self, request: web.Request) -> web.Response:
        body = await request.json()
        key = str(body.get("key") or "").strip()
        if len(key) > 200:
            raise web.HTTPBadRequest(text="That doesn't look like an API key.")
        error = await self.fw.vchart.check_mml_key(key) if key else None
        if error:
            return web.json_response({"ok": False, "error": error})
        self.fw.set_mml_key(key)
        return web.json_response({"ok": True})

    # --- routes, weather stations and harbours -----------------------------

    async def routes(self, request: web.Request) -> web.Response:
        return web.json_response(self.fw.store.kv_get("routes", []))

    async def save_route(self, request: web.Request) -> web.Response:
        """A new route, or changes to a saved one (by id): its name, points, speed or whether it's shown."""
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Expected a route")
        routes = self.fw.store.kv_get("routes", [])
        route = None
        if "id" in body:
            route = next((r for r in routes if r["id"] == body["id"]), None)
            if route is None:
                raise web.HTTPNotFound()
        elif "points" not in body:
            raise web.HTTPBadRequest(text="A route needs points")
        elif len(routes) >= MAX_ROUTES:
            raise web.HTTPBadRequest(text=f"At most {MAX_ROUTES} routes can be saved")
        else:
            route = {"id": max((r["id"] for r in routes), default=0) + 1, "shown": True, "speed": 25.0,
                     "created": int(time.time())}
            routes.append(route)
        if "points" in body:
            points = _points(body["points"], 2)
            if len(points) > MAX_ROUTE_POINTS:
                raise web.HTTPBadRequest(text=f"At most {MAX_ROUTE_POINTS} points")
            route["points"] = [[round(lat, 6), round(lon, 6)] for lat, lon in points]
        if "name" in body or "name" not in route:
            route["name"] = str(body.get("name") or "").strip()[:60] or f"Route {route['id']}"
        if "speed" in body:
            try:
                speed = float(body["speed"])
            except (TypeError, ValueError):
                speed = math.nan
            if not 0.5 <= speed <= 60:
                raise web.HTTPBadRequest(text="Speed must be 0.5–60 kn")
            route["speed"] = round(speed, 1)
        if "shown" in body:
            route["shown"] = bool(body["shown"])
        self.fw.store.kv_set("routes", routes)
        return web.json_response(route)

    async def delete_route(self, request: web.Request) -> web.Response:
        try:
            route_id = int(request.match_info["route_id"])
        except ValueError:
            raise web.HTTPNotFound() from None
        routes = self.fw.store.kv_get("routes", [])
        kept = [r for r in routes if r["id"] != route_id]
        if len(kept) == len(routes):
            raise web.HTTPNotFound()
        self.fw.store.kv_set("routes", kept)
        return web.json_response({"ok": True})

    async def autoroute(self, request: web.Request) -> web.Response:
        def point(name: str) -> tuple[float, float]:
            try:
                lat, lon = (float(v) for v in request.query[name].split(","))
            except (KeyError, ValueError):
                raise web.HTTPBadRequest(text="from and to are needed as lat,lon") from None
            if not (-85 <= lat <= 85 and -180 <= lon <= 180):
                raise web.HTTPBadRequest(text="Point out of range")
            return lat, lon

        start, end = point("from"), point("to")
        try:
            draught = float(request.query.get("draught", 1.5))
        except ValueError:
            draught = math.nan
        if not 0 <= draught <= 15:
            raise web.HTTPBadRequest(text="Draught must be 0–15 m")
        return web.json_response(await self.fw.autoroute.route(start, end, draught))

    async def stations(self, request: web.Request) -> web.Response:
        stations = await self.fw.stations.latest()
        return web.json_response({"stations": stations, "error": self.fw.stations.error,
                                  "fetched": self.fw.stations.fetched or None})

    async def station_history(self, request: web.Request) -> web.Response:
        try:
            fmisid = int(request.match_info["fmisid"])
        except ValueError:
            raise web.HTTPNotFound() from None
        points = await self.fw.stations.history(fmisid)
        if points is None:
            raise web.HTTPNotFound()
        return web.json_response({"points": points})

    async def harbours(self, request: web.Request) -> web.Response:
        try:
            west, south, east, north = (float(v) for v in request.query["bbox"].split(","))
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="bbox=west,south,east,north is needed") from None
        if not (south <= north and west <= east) or (north - south) * (east - west) > 4:
            raise web.HTTPBadRequest(text="The box is too big; zoom in")
        return web.json_response(self.fw.harbours.in_view(south, west, north, east))

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
        self.fw.panel_opened()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.sockets.discard(ws)
        return ws

    def on_event(self, kind: str, payload: dict[str, Any]) -> None:
        if self.sockets:
            self._send({"type": kind, "ship": {**payload, "next_passage": self.fw.predictor.get(payload["mmsi"])}})

    def on_fleet_event(self, kind: str, payload: dict[str, Any]) -> None:
        # Fleet ships report every few seconds; the panel gets at most one position per ship
        # per FLEET_PUSH_SECONDS (the latest one, once the time is up). My own ships, and so
        # the alert ferries, go through on_event at full rate.
        if not self.sockets:
            return
        mmsi = payload["mmsi"]
        if kind == "position":
            wait = self._fleet_pushed.get(mmsi, 0.0) + FLEET_PUSH_SECONDS - time.monotonic()
            if wait > 0:
                if mmsi not in self._fleet_pending:
                    self._fleet_pending.add(mmsi)
                    asyncio.get_running_loop().call_later(wait, self._push_fleet, mmsi, kind)
                return
        self._push_fleet(mmsi, kind)

    def _push_fleet(self, mmsi: int, kind: str) -> None:
        self._fleet_pending.discard(mmsi)
        if mmsi not in self.fw.fleet_tracker.ships:
            return
        self._fleet_pushed[mmsi] = time.monotonic()
        self._send({"type": kind, "ship": self.fw.fleet_summary(mmsi)})

    def broadcast_aircraft(self, aircraft: list[dict[str, Any]]) -> None:
        self._send({"type": "aircraft", "aircraft": aircraft})

    def broadcast(self, message: dict[str, Any]) -> None:
        if self.sockets:
            self._send(message)

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
