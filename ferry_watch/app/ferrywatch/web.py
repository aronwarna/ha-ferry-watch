"""HTTP API and panel, served through Home Assistant ingress.

All URLs in the panel are relative, because ingress serves it under a
per-session path prefix.
"""

from __future__ import annotations

import asyncio
import os
import base64
import gzip
import hashlib
import hmac
import json
import logging
import math
import re
import tempfile
import time
import weakref
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import WSMsgType, web

from . import __version__, boats, config, dayscore, diagnostics, notices, satellite, stats
from .airhistory import PERIODS
from .charts import in_finnish_waters
from .config import DEV
from .notify import sun_place
from .fleets import FLEET_IDS
from .tracker import STOP_SPEED
from .vchart import MIN_Z, WATER_MAX_Z

SAMPLES_KEPT = 10  # chart samples kept in the media folder (newest)
SAMPLE_NAME = re.compile(r"chart-[0-9.-]+N-[0-9.-]+E-[0-9-]+\.json")
TILE_WAIT = 0.3  # s a drawn-chart tile request may wait for its data before answering "pending"

if TYPE_CHECKING:
    from .__main__ import FerryWatch

_LOGGER = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml",
                ".json": "application/json"}
INGRESS_PROXY = "172.30.32.2"
TAIL_MAX_MINUTES = 24 * 60
# A passage replay shows its whole trip, port to port, but no more than this either side.
REPLAY_MAX_SPAN = 18 * 3600
FLEET_PUSH_SECONDS = 5


AUTH_COOKIE = "fw_auth"
# Loaded by the phone's notification, which can't log in; and the container's health check.
OPEN_PATHS = ("/api/notify/picture/", "/api/health")
PICTURE_NAME = re.compile(r"passage-[A-Za-z0-9_-]+\.png")


def _cookie_value(password: str) -> str:
    return hmac.new(password.encode(), b"ferry-watch-panel", hashlib.sha256).hexdigest()


def _logged_in(request: web.Request, password: str) -> bool:
    """The password, from the browser's own login box (HTTP Basic, any user name) or from
    the cookie set after it, which the live connection (a websocket) carries too."""
    if hmac.compare_digest(request.cookies.get(AUTH_COOKIE, ""), _cookie_value(password)):
        return True
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        given = base64.b64decode(value).decode().partition(":")[2]
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(given.encode(), password.encode())


@web.middleware
async def ingress_only(request: web.Request, handler):
    if DEV:
        return await handler(request)
    if config.STANDALONE:
        # A plain container: whoever reaches the port gets the panel, so it belongs on a
        # private network (the LAN, Tailscale). FW_PASSWORD adds a password on top.
        password = config.PASSWORD
        if not password or request.path.startswith(OPEN_PATHS):
            return await handler(request)
        if not _logged_in(request, password):
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="Ferry Watch"'})
        resp = await handler(request)
        if AUTH_COOKIE not in request.cookies and not resp.prepared:
            resp.set_cookie(AUTH_COOKIE, _cookie_value(password), max_age=365 * 86400, httponly=True, samesite="Lax")
        return resp
    # Only the Supervisor's ingress proxy may reach the panel (it handles HA login).
    if request.remote not in (INGRESS_PROXY, "127.0.0.1", "::1"):
        raise web.HTTPForbidden()
    return await handler(request)


PREF_MAX_KEYS, PREF_MAX_LEN = 200, 2000
ZONE_ROLES = {"inner": "Inner route", "outer": "Outer route", "phone": "My area"}
MAX_ROUTES = 100
MAX_ROUTE_POINTS = 500


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


def _route_plan(value: Any, n: int) -> dict[str, list[int]] | None:
    """A saved route's plan: wp, the indexes of its stops among the points (first and last
    included, rising); fw, 1 for each leg between stops that follows the fairways and 2 for one planned over open water; bend,
    which of the stops (as indexes into wp, neither end) are bends the user pulled the line
    to rather than stops of their own; and via, which are points on a fairway they dragged
    the route onto. None for none, or one that doesn't fit the points."""
    if not isinstance(value, dict):
        return None
    wp, fw = value.get("wp"), value.get("fw")
    if not isinstance(wp, list) or not isinstance(fw, list) or len(wp) < 2 or len(fw) != len(wp) - 1:
        return None
    if not all(type(v) is int for v in wp) or not all(v in (0, 1, 2) and type(v) is int for v in fw):
        return None
    if wp[0] != 0 or wp[-1] != n - 1 or any(b <= a for a, b in zip(wp, wp[1:])):
        return None
    marks = {}
    for name in ("bend", "via"):
        got = value.get(name) or []
        if not isinstance(got, list) or not all(type(v) is int and 0 < v < len(wp) - 1 for v in got) \
                or any(b <= a for a, b in zip(got, got[1:])):
            return None
        if got:
            marks[name] = got
    if set(marks.get("bend", ())) & set(marks.get("via", ())):
        return None
    if not any(fw) and not marks:
        return None
    return {"wp": wp, "fw": fw, **marks}


def route_to_gpx(route: dict[str, Any]) -> str:
    esc = lambda text: (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))  # noqa: E731
    points = route["points"]
    wp = set((route.get("plan") or {}).get("wp") or range(len(points)))
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="Ferry Watch" xmlns="http://www.topografix.com/GPX/1/1">',
             f'  <metadata><name>{esc(route["name"])}</name><desc>Planning only, not for navigation</desc></metadata>',
             f'  <rte><name>{esc(route["name"])}</name>']
    stop = 0
    for i, (lat, lon) in enumerate(points):
        # The user's own stops are numbered; the turns of a fairway leg between them aren't.
        if i in wp:
            stop += 1
            label = f"{stop:02d}"
        else:
            label = f"t{i:03d}"
        lines.append(f'    <rtept lat="{lat:.6f}" lon="{lon:.6f}"><name>{label}</name></rtept>')
    lines += ["  </rte>", "</gpx>", ""]
    return "\n".join(lines)


def routes_from_gpx(body: bytes) -> list[tuple[str, list[list[float]]]]:
    """[(name, [[lat, lon], ...])] for each route in a GPX file, or each track when it has
    no routes; long ones thinned evenly to MAX_ROUTE_POINTS."""
    root = ET.fromstring(body)
    local = lambda el: el.tag.rsplit("}", 1)[-1]  # noqa: E731
    out = []
    for kind, pt in (("rte", "rtept"), ("trk", "trkpt")):
        for el in root.iter():
            if local(el) != kind:
                continue
            name = next((c.text or "" for c in el if local(c) == "name"), "").strip()
            points = []
            for p in el.iter():
                if local(p) != pt:
                    continue
                try:
                    lat, lon = float(p.get("lat")), float(p.get("lon"))
                except (TypeError, ValueError):
                    continue
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    points.append([round(lat, 6), round(lon, 6)])
            if len(points) > MAX_ROUTE_POINTS:
                step = (len(points) - 1) / (MAX_ROUTE_POINTS - 1)
                points = [points[round(i * step)] for i in range(MAX_ROUTE_POINTS)]
            if len(points) >= 2:
                out.append((name, points))
        if out:
            break
    return out


def _mmsi(request: web.Request) -> int:
    try:
        return int(request.match_info["mmsi"])
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid MMSI") from None


def _image_type(path) -> str:
    """JPEG or PNG, from the file's first bytes (aerial photos come as either)."""
    with open(path, "rb") as f:
        return "image/jpeg" if f.read(3) == b"\xff\xd8\xff" else "image/png"


LOGBOOK_MAX_BYTES = 200 * 1024 * 1024


def _read_logbook_file(logbook, f, name: str) -> list[dict]:
    """One uploaded file: GPX read in, a .zip opened for the GPX files in it, anything
    else answered with why it can't be read."""
    head = f.read(4)
    f.seek(0)
    low = name.lower()
    if head[:2] == b"PK":
        out = []
        try:
            with zipfile.ZipFile(f) as z:
                for info in z.infolist():
                    if info.filename.lower().endswith(".gpx") and not info.is_dir():
                        with z.open(info) as g:
                            out.extend(_read_logbook_file(logbook, g, f"{name}/{info.filename}"))
        except zipfile.BadZipFile:
            return [{"file": name, "error": "This zip file is damaged."}]
        return out or [{"file": name, "error": "No GPX files in this zip."}]
    if low.endswith(".adm"):
        return [{"file": name, "error": "ADM is Garmin's own format, which only Garmin's apps read. "
                 "On the plotter, choose GPX when saving to the card."}]
    if low.endswith(".rsd"):
        return [{"file": name, "error": "Sonar recordings (.RSD) are not read yet. Upload the GPX track for the logbook."}]
    if low.endswith(".fit"):
        return [{"file": name, "error": "FIT files are not read yet. Save the track as GPX on the plotter."}]
    try:
        return [logbook.add_gpx(f, name)]
    except ET.ParseError as err:
        return [{"file": name, "error": f"Not a GPX file the logbook can read ({err})."}]


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
        r.add_get("/api/health", self.health)
        r.add_get("/api/prefs", self.prefs)
        r.add_post("/api/prefs", self.set_prefs)
        r.add_get("/api/diagnostics", self.diagnostics)
        r.add_get("/api/changelog", self.changelog)
        r.add_get("/api/tails", self.tails)
        r.add_get("/api/track/{mmsi}", self.track)
        r.add_get("/api/photo/{mmsi}", self.photo)
        r.add_get("/api/vessels/{mmsi}/photo", self.vessel_photo)
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
        r.add_get("/api/notify/picture/{name}", self.notify_picture)
        r.add_post("/api/fleets/{fleet_id}", self.set_fleet)
        r.add_get("/api/tiles/{source}/{z}/{x}/{y}", self.tile)
        r.add_get("/api/mapsources", self.mapsources)
        r.add_get("/api/here", self.here)
        r.add_get("/api/pins", self.pins)
        r.add_post("/api/pins", self.save_pin)
        r.add_delete("/api/pins/{pin_id}", self.delete_pin)
        r.add_get("/api/pins.gpx", self.pins_gpx)
        r.add_get("/api/places", self.places)
        r.add_get("/api/maptile/{layer}/{z}/{x}/{y}", self.map_tile)
        r.add_get("/api/buildings/{x}/{y}", self.buildings_tile)
        r.add_get("/api/vchart/{z}/{x}/{y}", self.vchart_tile)
        r.add_get("/api/vchart-water/{z}/{x}/{y}", self.vchart_water)
        r.add_get("/api/overlays", self.overlays)
        r.add_get("/api/overlay-value/{overlay_id}", self.overlay_value)
        r.add_get("/api/sky", self.sky)
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
        r.add_get("/api/notices/nautical", self.nautical_warnings)
        r.add_get("/api/notices/weather", self.weather_warnings)
        r.add_get("/api/notices/structures", self.structures)
        r.add_post("/api/route-limits", self.route_limits)
        r.add_get("/api/outlook", self.outlook)
        r.add_get("/api/traffic", self.traffic)
        r.add_get("/api/ports/{mmsi}", self.port_times)
        r.add_post("/api/vchart/keep", self.set_vchart_keep)
        r.add_post("/api/vchart/foreign", self.set_vchart_foreign)
        r.add_post("/api/vchart/key", self.set_mml_key)
        r.add_post("/api/follow", self.follow)
        r.add_delete("/api/follow/{mmsi}", self.unfollow)
        r.add_get("/api/routes", self.routes)
        r.add_post("/api/routes", self.save_route)
        r.add_delete("/api/routes/{route_id}", self.delete_route)
        r.add_get("/api/routes/{route_id}.gpx", self.route_gpx)
        r.add_post("/api/routes/import", self.import_routes)
        r.add_post("/api/day-score", self.day_score)
        r.add_get("/api/autoroute", self.autoroute)
        r.add_post("/api/smartroute", self.smartroute)
        r.add_get("/api/boats", self.boats)
        r.add_get("/api/speed-limits", self.speed_limits)
        r.add_get("/api/fairways", self.fairways)
        r.add_get("/api/stations", self.stations)
        r.add_get("/api/stations/{fmisid}/history", self.station_history)
        r.add_get("/api/harbours", self.harbours)
        r.add_get("/api/reserves", self.reserves)
        r.add_get("/api/logbook", self.logbook)
        r.add_post("/api/logbook/upload", self.logbook_upload)
        r.add_get("/api/logbook/{trip_id}", self.logbook_trip)
        r.add_post("/api/logbook/{trip_id}", self.logbook_edit)
        r.add_delete("/api/logbook/{trip_id}", self.logbook_delete)
        r.add_get("/static/{path:.+}", self.static_file)
        self._static_gz: dict[str, tuple[int, bytes]] = {}  # path -> (mtime ns, gzipped body)
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
        # (Every script and stylesheet, the map library's included: with the version on, the
        # browser may keep the file for good, see static_file.)
        html = re.sub(r'(src|href)="(static/[^"?]+\.(?:js|css))"', rf'\1="\2?v={__version__}"', html)
        # What is switched on, kept on the NAS so the phone and the desktop agree
        # (static/prefs.js puts it in place before the other scripts read it).
        prefs = json.dumps(self.fw.store.kv_get("prefs", {})).replace("<", "\\u003c")
        html = html.replace("<head>", f"<head>\n  <script>window.FW_PREFS = {prefs};</script>", 1)
        # Ingress serves the panel under a per-session prefix; a <base> keeps relative
        # URLs working on deep links like /ship/<mmsi>.
        prefix = request.headers.get("X-Ingress-Path", "").rstrip("/")
        base = f"{prefix}/" if prefix or request.path != "/" else ""
        if base:
            html = html.replace("<head>", f'<head>\n  <base href="{base}">', 1)
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def static_file(self, request: web.Request) -> web.StreamResponse:
        """The panel's own files. Scripts and styles go out gzipped (1.3 MB becomes 0.4), and
        one asked for with its version (?v=) is kept by the browser for good: the next
        version has another address. The gzipped copies are made once and kept (~0.4 MB)."""
        rel = request.match_info["path"]
        path = (STATIC / rel).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            raise web.HTTPNotFound()
        headers = {"Cache-Control": "public, max-age=31536000, immutable" if "v" in request.query else "no-cache"}
        kind = STATIC_TYPES.get(path.suffix)
        if not kind or "gzip" not in request.headers.get("Accept-Encoding", ""):
            return web.FileResponse(path, headers=headers)
        mtime = path.stat().st_mtime_ns
        etag = f'"{mtime:x}"'
        headers.update({"ETag": etag, "Vary": "Accept-Encoding"})
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        kept = self._static_gz.get(rel)
        if not kept or kept[0] != mtime:
            kept = (mtime, await asyncio.to_thread(lambda: gzip.compress(path.read_bytes(), 6)))
            self._static_gz[rel] = kept
        return web.Response(body=kept[1], content_type=kind, charset="utf-8",
                            headers={**headers, "Content-Encoding": "gzip"})

    async def prefs(self, request: web.Request) -> web.Response:
        return web.json_response(self.fw.store.kv_get("prefs", {}), headers={"Cache-Control": "no-store"})

    async def set_prefs(self, request: web.Request) -> web.Response:
        """Changed preferences {key: text, or null to forget it}; only the panel's own fw- keys."""
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Expected an object")
        prefs = dict(self.fw.store.kv_get("prefs", {}))
        for key, value in body.items():
            if not isinstance(key, str) or not key.startswith("fw-") or len(key) > 64:
                raise web.HTTPBadRequest(text="Not a preference")
            if value is None:
                prefs.pop(key, None)
            elif isinstance(value, str) and len(value) <= PREF_MAX_LEN:
                prefs[key] = value
            else:
                raise web.HTTPBadRequest(text="Preferences are short texts")
        if len(prefs) > PREF_MAX_KEYS:
            raise web.HTTPBadRequest(text="Too many preferences")
        self.fw.store.kv_set("prefs", prefs)
        return web.json_response({"ok": True})

    async def diagnostics(self, request: web.Request) -> web.Response:
        fw = self.fw
        listing = await fw.overlays.listing(set()) if fw.overlays else []
        secrets = [config.HA_TOKEN, config.PASSWORD, config.NTFY_URL, fw.store.kv_get("mml_api_key")]
        text = diagnostics.report(await fw.state(), listing, secrets)
        return web.Response(text=text, headers={"Cache-Control": "no-store"})

    async def changelog(self, request: web.Request) -> web.Response:
        """What changed up to this version since the one the panel last saw (?since=)."""
        for path in (Path(__file__).parents[1] / "CHANGELOG.md", Path(__file__).parents[2] / "CHANGELOG.md"):
            if path.is_file():
                text = await asyncio.to_thread(path.read_text)
                break
        else:
            text = ""
        since = request.query.get("since") or None
        return web.json_response({"version": __version__, "sections": diagnostics.whats_new(text, __version__, since)},
                                 headers={"Cache-Control": "no-store"})

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "version": __version__})

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
        if "around" in request.query:
            # A recorded passage: the whole trip it was part of, port to port.
            try:
                around = int(request.query["around"])
            except ValueError:
                raise web.HTTPBadRequest(text="Invalid time") from None
            since, until = self.fw.store.voyage_around(mmsi, around, STOP_SPEED, REPLAY_MAX_SPAN)
            points = self.fw.store.track(mmsi, since, until, speed=True)
            return web.json_response({"mmsi": mmsi, "since": since, "until": until, "points": points})
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
        if source == "photo":  # Satellite view: a country's own aerial photos over Esri's
            path = await self.fw.satellite.tile(z, x, y) if self.fw.satellite else None
            if not path:  # elsewhere, or no photo here: Esri's imagery underneath shows
                cover = satellite.region(z, x, y)  # outside the covered countries: for good
                raise web.HTTPNotFound(headers={"Cache-Control": "no-store" if cover else "max-age=604800"})
            return web.FileResponse(path, headers={"Cache-Control": "max-age=604800", "Content-Type": _image_type(path)})
        if source == "seamark-abroad":  # chart view: the Finnish charts draw their own marks
            if in_finnish_waters(z, x, y):
                raise web.HTTPNotFound(headers={"Cache-Control": "max-age=604800"})
            source = "seamark"
        url = self.fw.tile_url(source, z, x, y)
        if not url and source == "topo":  # outside Finland, for good: the plain map shows there
            raise web.HTTPNotFound(headers={"Cache-Control": "max-age=604800"})
        if not url or not (0 <= z <= 20 and 0 <= x < 2**z and 0 <= y < 2**z):
            raise web.HTTPNotFound()
        if source == "terrain" and os.environ.get("FW_SIMULATE") == "1":
            from .simweather import terrain_tile  # made-up hills and sea, offline
            body = await asyncio.to_thread(terrain_tile, z, x, y)
            return web.Response(body=body, content_type="image/png", headers={"Cache-Control": "max-age=86400"})
        path = await self.fw.tiles.get(source, z, x, y, url)
        if not path:
            raise web.HTTPNotFound(headers={"Cache-Control": "no-store"})  # may just be a failed fetch
        return web.FileResponse(path, headers={"Cache-Control": "max-age=604800", "Content-Type": _image_type(path)})

    async def here(self, request: web.Request) -> web.Response:
        """What's here: depth, weather and seabed at a long-pressed place (here.py)."""
        try:
            lat, lon = float(request.query["lat"]), float(request.query["lon"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest() from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not self.fw.here:
            raise web.HTTPBadRequest()
        return web.json_response(await self.fw.here.at(lat, lon), headers={"Cache-Control": "no-store"})

    # --- your own pins, and place search (pins.py) -------------------------------------

    async def pins(self, request: web.Request) -> web.Response:
        return web.json_response(self.fw.store.kv_get("pins", []) or [], headers={"Cache-Control": "no-store"})

    async def save_pin(self, request: web.Request) -> web.Response:
        from .pins import MAX_PINS, clean_pin
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest() from None
        rows = list(self.fw.store.kv_get("pins", []) or [])
        old = next((p for p in rows if body.get("id") and p.get("id") == body["id"]), None)
        try:
            pin = clean_pin(body, old)
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from None
        if old:
            rows[rows.index(old)] = pin
        elif len(rows) >= MAX_PINS:
            raise web.HTTPBadRequest(text="Too many pins")
        else:
            rows.append(pin)
        self.fw.store.kv_set("pins", rows)
        return web.json_response(pin)

    async def delete_pin(self, request: web.Request) -> web.Response:
        rows = [p for p in (self.fw.store.kv_get("pins", []) or []) if p.get("id") != request.match_info["pin_id"]]
        self.fw.store.kv_set("pins", rows)
        return web.json_response({"ok": True})

    async def pins_gpx(self, request: web.Request) -> web.Response:
        from .pins import pins_gpx
        return web.Response(text=pins_gpx(self.fw.store.kv_get("pins", []) or []), content_type="application/gpx+xml",
                            headers={"Content-Disposition": 'attachment; filename="ferry-watch-pins.gpx"'})

    async def places(self, request: web.Request) -> web.Response:
        from .pins import parse_coords
        q = request.query.get("q", "")
        coords = parse_coords(q)
        rows = [] if coords else await self.fw.places.search(q)
        return web.json_response({"coords": coords, "places": rows, "error": self.fw.places.error})

    async def mapsources(self, request: web.Request) -> web.Response:
        """The extra layers (old photos and maps, overlays) the panel can show."""
        return web.json_response(self.fw.maps.listing(), headers={"Cache-Control": "no-store"})

    async def map_tile(self, request: web.Request) -> web.StreamResponse:
        try:
            z, x = int(request.match_info["z"]), int(request.match_info["x"])
            y = int(request.match_info["y"].split(".")[0])
        except ValueError:
            raise web.HTTPBadRequest() from None
        layer = request.match_info["layer"]
        cover = self.fw.maps.covers(layer, z, x, y)
        if not cover:  # outside the layer's area or zooms: for good
            raise web.HTTPNotFound(headers={"Cache-Control": "max-age=604800" if cover is False else "no-store"})
        path = await self.fw.maps.tile(layer, z, x, y)
        if not path:
            raise web.HTTPNotFound(headers={"Cache-Control": "no-store"})  # may just be a failed fetch
        daily = (self.fw.maps.row(layer) or {}).get("daily")
        return web.FileResponse(path, headers={"Cache-Control": "max-age=21600" if daily else "max-age=2592000",
                                               "Content-Type": _image_type(path)})

    async def buildings_tile(self, request: web.Request) -> web.Response:
        try:
            x, y = int(request.match_info["x"]), int(request.match_info["y"])
        except ValueError:
            raise web.HTTPBadRequest() from None
        body = await self.fw.buildings.tile(x, y)
        if body is None:
            raise web.HTTPNotFound(headers={"Cache-Control": "max-age=86400"})
        return web.Response(body=body, content_type="application/json",
                            headers={"Content-Encoding": "gzip", "Cache-Control": "max-age=86400"})

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
        # A picture drawn before all it needed was there (the chart's water still coming).
        cache = "no-store" if path.name.endswith(".partial.png") else f"max-age={age}"
        return web.FileResponse(path, headers={"Cache-Control": cache, "Content-Type": "image/png"})

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

    async def overlay_value(self, request: web.Request) -> web.Response:
        try:
            lat, lon = float(request.query["lat"]), float(request.query["lon"])
            ts = int(float(request.query.get("t") or time.time()))
        except (KeyError, ValueError):
            raise web.HTTPBadRequest() from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise web.HTTPBadRequest()
        data = await self.fw.overlays.value(request.match_info["overlay_id"], lat, lon, ts)
        if data is None:
            raise web.HTTPNotFound(headers={"Cache-Control": "no-store"})
        return web.json_response(data, headers={"Cache-Control": "max-age=300"})

    async def sky(self, request: web.Request) -> web.Response:
        """For the sky map's night: the weather model's clouds at a place over the next day
        (with clouds=1) and the Kp forecast."""
        try:
            lat, lon = float(request.query["lat"]), float(request.query["lon"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest() from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise web.HTTPBadRequest()
        ov, now = self.fw.overlays, time.time()
        out: dict[str, Any] = {"clouds": [], "kp": []}
        try:
            if request.query.get("clouds") == "1" and "forecast" in ov.registry:
                out["clouds"] = await ov.series("forecast", lat, lon, ["cloud", "low", "mid", "high"],
                                                now - 3600, now + 30 * 3600)
            if "aurora" in ov.registry:
                run = await ov.run_for("aurora", now)
                out["kp"] = (run or {}).get("info", {}).get("kp", [])
        except Exception as err:  # noqa: BLE001 - the sky map works without them
            out["error"] = str(err) or type(err).__name__
        return web.json_response(out, headers={"Cache-Control": "max-age=600"})

    async def vchart_tile(self, request: web.Request) -> web.Response:
        return await self._chart_tile(request, self.fw.vchart.tile_gz, 20)

    async def vchart_water(self, request: web.Request) -> web.Response:
        """A chart tile's water alone, for cutting the sea layers to it (see fields.js)."""
        return await self._chart_tile(request, self.fw.vchart.water_gz, WATER_MAX_Z)

    async def _chart_tile(self, request: web.Request, get: Callable, max_z: int) -> web.Response:
        try:
            z, x = int(request.match_info["z"]), int(request.match_info["x"])
            y = int(request.match_info["y"].split(".")[0])
        except ValueError:
            raise web.HTTPBadRequest() from None
        if not (MIN_Z <= z <= max_z and 0 <= x < 2**z and 0 <= y < 2**z):
            raise web.HTTPNotFound()
        result = await get(z, x, y, wait=TILE_WAIT)
        if result is None:
            # Still downloading: answer now and let the panel ask again, so no
            # request sits open long enough to block the browser's other ones.
            return web.json_response({"pending": True}, status=202, headers={"Cache-Control": "no-store"})
        body, complete = result
        # A tile missing a block that couldn't be fetched is asked for again next time.
        headers = {"Cache-Control": "max-age=86400" if complete else "no-store", "Vary": "Accept-Encoding",
                   "X-Chart-Complete": "1" if complete else "0"}
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            headers["Content-Encoding"] = "gzip"  # tiles are kept gzipped: sent as they are
        else:
            body = await asyncio.to_thread(gzip.decompress, body)
        return web.Response(body=body, content_type="application/json", headers=headers)

    async def photo(self, request: web.Request) -> web.StreamResponse:
        path = self.fw.photo_path(_mmsi(request))
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})

    async def vessel_photo(self, request: web.Request) -> web.Response:
        """Looks up (once) and describes a photo of any vessel; the image is at api/photo/<mmsi>."""
        mmsi = _mmsi(request)
        photo = await self.fw.any_ship_photo(mmsi)
        return web.json_response({"photo": photo, "miss": None if photo else self.fw.ship_photo_miss(mmsi)})

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

    @staticmethod
    def _passage_filters(request: web.Request) -> dict[str, Any]:
        """Which passages a list or CSV asks for: route, direction (east/west), since and
        until (Unix times), mmsi (comma separated)."""
        q = request.query
        try:
            return {
                "route": q.get("route") or None,
                "direction": q.get("direction") or None,
                "since_ts": int(q["since"]) if q.get("since") else None,
                "before_ts": int(q["until"]) if q.get("until") else None,
                "mmsis": [int(m) for m in q["mmsi"].split(",") if m] if q.get("mmsi") else None,
            }
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid filter") from None

    async def passages(self, request: web.Request) -> web.Response:
        try:
            limit = min(int(request.query.get("limit", 30)), 500)
            offset = int(request.query.get("offset", 0))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid limit") from None
        names = stats.ship_names(self.fw.store)
        rows = self.fw.store.passages(limit=limit, offset=offset, **self._passage_filters(request))
        for row in rows:
            info = names.get(row["mmsi"], {})
            row["name"] = info.get("name", f"MMSI {row['mmsi']}")
            row["color"] = info.get("color", "#64748b")
        return web.json_response(rows)

    async def passages_csv(self, request: web.Request) -> web.Response:
        return web.Response(
            text=stats.to_csv(self.fw.store, **self._passage_filters(request)), content_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="ferry-watch-passages.csv"'},
        )

    async def stats(self, request: web.Request) -> web.Response:
        # Every passage there is, so it goes out gzipped (about a sixth of the size), and not
        # at all when the panel already has this very answer.
        body = json.dumps(stats.payload(self.fw.store)).encode()
        etag = f'"{hashlib.sha1(body).hexdigest()[:20]}"'
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        resp = web.Response(body=body, content_type="application/json", headers=headers)
        resp.enable_compression()
        return resp

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

    async def nautical_warnings(self, request: web.Request) -> web.Response:
        n = self.fw.notices
        return web.json_response({"warnings": await n.nautical.get(), **n.nautical.status()},
                                 headers={"Cache-Control": "no-store"})

    async def weather_warnings(self, request: web.Request) -> web.Response:
        """Sea weather warnings with their areas, and which of them hold the place asked
        about (?at=lat,lon; else the middle of my area, or Porkkala)."""
        n = self.fw.notices
        warnings = await n.weather.get()
        try:
            lat, lon = (float(v) for v in request.query["at"].split(","))
        except (KeyError, ValueError):
            lat, lon = sun_place(self.fw.store)
        return web.json_response({"warnings": warnings, "here": notices.warnings_at(warnings, lat, lon),
                                  "at": [lat, lon], **n.weather.status()}, headers={"Cache-Control": "no-store"})

    async def structures(self, request: web.Request) -> web.Response:
        n = self.fw.notices
        resp = web.json_response({"items": await n.structures(), "error": n.structures_error},
                                 headers={"Cache-Control": "max-age=3600"})
        resp.enable_compression()
        return resp

    async def route_limits(self, request: web.Request) -> web.Response:
        """The speed limits a route runs through: {points, speed (kn)}."""
        body = await request.json()
        points = _points(body.get("points") if isinstance(body, dict) else None, 2)
        try:
            speed = float(body.get("speed")) if body.get("speed") is not None else None
        except (TypeError, ValueError):
            speed = None
        try:
            out = await self.fw.notices.route_limits(points, speed if speed and 0 < speed <= 80 else None)
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from None
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise web.HTTPBadGateway(text=f"Couldn't read the speed limits: {err}") from None
        return web.json_response(out, headers={"Cache-Control": "no-store"})

    async def faults(self, request: web.Request) -> web.Response:
        return web.json_response({"faults": await self.fw.faults.get(), "error": self.fw.faults.error})

    async def outlook(self, request: web.Request) -> web.Response:
        ships = {s["mmsi"]: s for s in self.fw.store.ships()}
        return web.json_response(await self.fw.outlook.payload(ships))

    async def port_times(self, request: web.Request) -> web.Response:
        try:
            mmsi = int(request.match_info["mmsi"])
        except ValueError:
            raise web.HTTPBadRequest(text="mmsi must be a number")
        return web.json_response(self.fw.port_times(mmsi))

    async def traffic(self, request: web.Request) -> web.Response:
        if "s" in request.query:  # a wide view: every ship inside the box
            try:
                box = [float(request.query[k]) for k in ("s", "w", "n", "e")]
            except (KeyError, ValueError):
                raise web.HTTPBadRequest(text="s, w, n and e are needed")
            if any(v != v for v in box):
                raise web.HTTPBadRequest(text="s, w, n and e are needed")
            ships = await self.fw.traffic.in_view(*box)
            resp = web.json_response({"ships": ships, "error": self.fw.traffic.error})
            if len(ships) > 200:
                resp.enable_compression()  # ~70 bytes a ship, a quarter of that gzipped
            return resp
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

    async def day_score(self, request: web.Request) -> web.Response:
        """The boating day score for a route: good, okay or bad to set off each hour of the
        next two days, at the given speed (knots) for a boat size (see dayscore.py)."""
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="Expected a route") from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Expected a route")
        points = _points(body.get("points"), 2)
        try:
            speed = float(body.get("speed") or 25)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="Speed must be a number") from None
        if not 0.5 <= speed <= 60:
            raise web.HTTPBadRequest(text="Speed must be 0.5-60 kn")
        try:
            out = await self.fw.dayscore.score(points, speed, str(body.get("boat") or dayscore.DEFAULT_BOAT))
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from None
        return web.json_response(out, headers={"Cache-Control": "no-store"})

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
            # Which points are the user's stops and which legs follow the fairways, so the
            # route tool can open it for editing as it was drawn.
            plan = _route_plan(body.get("plan"), len(points))
            if plan:
                route["plan"] = plan
            else:
                route.pop("plan", None)
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

    async def route_gpx(self, request: web.Request) -> web.Response:
        """A saved route as a GPX route, which chart plotters read (Garmin: copy it to the
        card's Garmin/UserData folder and import user data)."""
        try:
            route_id = int(request.match_info["route_id"])
        except ValueError:
            raise web.HTTPNotFound() from None
        route = next((r for r in self.fw.store.kv_get("routes", []) if r["id"] == route_id), None)
        if not route:
            raise web.HTTPNotFound()
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", route["name"]).strip("-") or f"route-{route_id}"
        return web.Response(text=route_to_gpx(route), content_type="application/gpx+xml", headers={
            "Content-Disposition": f'attachment; filename="{name}.gpx"', "Cache-Control": "no-store"})

    async def import_routes(self, request: web.Request) -> web.Response:
        """Routes (or tracks, as routes) from an uploaded GPX file, saved like drawn ones."""
        body = await request.read()
        try:
            found = routes_from_gpx(body)
        except ET.ParseError as err:
            raise web.HTTPBadRequest(text=f"Not a GPX file ({err})") from None
        if not found:
            raise web.HTTPBadRequest(text="No route or track with at least two points in this file")
        routes = self.fw.store.kv_get("routes", [])
        if len(routes) + len(found) > MAX_ROUTES:
            raise web.HTTPBadRequest(text=f"At most {MAX_ROUTES} routes can be saved")
        added = []
        for name, points in found:
            route = {"id": max((r["id"] for r in routes), default=0) + 1, "shown": True, "speed": 25.0,
                     "created": int(time.time()), "points": points,
                     "name": name[:60] or f"Route {len(routes) + 1}"}
            routes.append(route)
            added.append(route)
        self.fw.store.kv_set("routes", routes)
        return web.json_response({"routes": added})

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
        # Which ends are via points the user put on a fairway (see autoroute.JOIN_ON_M).
        on = set(request.query.get("on", "").split(","))
        return web.json_response(await self.fw.autoroute.route(start, end, draught, ("from" in on, "to" in on)))

    async def smartroute(self, request: web.Request) -> web.Response:
        """A route over open water for a boat (see smartroute.py): {from, to: [lat, lon], boat,
        draught (m), speed (kn), depart (unix time), style ("fast" or "comfort", sailing)}."""
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="Expected a start and an end") from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Expected a start and an end")
        ends = _points([body.get("from"), body.get("to")], 2)

        def number(name: str, low: float, high: float) -> float | None:
            if body.get(name) in (None, ""):
                return None
            try:
                v = float(body[name])
            except (TypeError, ValueError):
                v = math.nan
            if not low <= v <= high:
                raise web.HTTPBadRequest(text=f"{name} must be {low:g}-{high:g}")
            return v

        out = await self.fw.smartroute.route(tuple(ends[0]), tuple(ends[1]), str(body.get("boat") or ""),
                                             number("draught", 0, 15), number("speed", 0.5, 60),
                                             number("depart", 0, 4e9), str(body.get("style") or "fast"))
        return web.json_response(out, headers={"Cache-Control": "no-store"})

    async def boats(self, request: web.Request) -> web.Response:
        """The boats the route tool plans for and every planning number (boats.py, smartroute.py)."""
        from . import smartroute
        out = boats.listing()
        out["planner"] = smartroute.numbers()
        return web.json_response(out, headers={"Cache-Control": "max-age=300"})

    async def speed_limits(self, request: web.Request) -> web.Response:
        """The speed limit areas in view: bbox=south,west,north,east."""
        try:
            south, west, north, east = (float(v) for v in request.query["bbox"].split(","))
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="bbox is needed as south,west,north,east") from None
        if not (-85 <= south < north <= 85 and -180 <= west < east <= 180):
            raise web.HTTPBadRequest(text="bbox out of range")
        try:
            areas = await self.fw.notices.limit_areas(south, west, north, east)
        except ValueError:
            return web.json_response({"areas": [], "too_big": True})
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise web.HTTPBadGateway(text=f"Couldn't read the speed limits: {err}") from None
        resp = web.json_response({"areas": areas}, headers={"Cache-Control": "max-age=600"})
        resp.enable_compression()
        return resp

    async def fairways(self, request: web.Request) -> web.Response:
        """The fairway lines in an area deep enough for a draught, for the route tool to snap
        a dragged route onto: bbox=south,west,north,east and draught."""
        try:
            south, west, north, east = (float(v) for v in request.query["bbox"].split(","))
            draught = float(request.query.get("draught", 1.5))
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="bbox is needed as south,west,north,east") from None
        if not (-85 <= south < north <= 85 and -180 <= west < east <= 180) or not 0 <= draught <= 15:
            raise web.HTTPBadRequest(text="bbox or draught out of range")
        return web.json_response(await self.fw.autoroute.lines(south, west, north, east, draught))

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
        """Harbours in a box (bbox=west,south,east,north), or by name (q=..., near=lat,lon)."""
        if "q" in request.query:  # by name, nearest first
            try:
                lat, lon = (float(v) for v in request.query.get("near", "60.1,24.9").split(","))
            except ValueError:
                raise web.HTTPBadRequest(text="near=lat,lon") from None
            return web.json_response(self.fw.harbours.search(request.query["q"], lat, lon))
        try:
            west, south, east, north = (float(v) for v in request.query["bbox"].split(","))
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="bbox=west,south,east,north is needed") from None
        if not (south <= north and west <= east) or (north - south) * (east - west) > 4:
            raise web.HTTPBadRequest(text="The box is too big; zoom in")
        return web.json_response(self.fw.harbours.in_view(south, west, north, east))

    # --- trip logbook (the Garmin plotter's tracks) -------------------------------------

    async def logbook(self, request: web.Request) -> web.Response:
        return web.json_response(await asyncio.to_thread(self.fw.logbook.listing))

    async def logbook_trip(self, request: web.Request) -> web.Response:
        trip = await asyncio.to_thread(self.fw.logbook.trip, request.match_info["trip_id"])
        if not trip:
            raise web.HTTPNotFound()
        return web.json_response(trip)

    async def logbook_upload(self, request: web.Request) -> web.Response:
        """GPX files from the plotter's card (several at once, or a .zip of them). Each is
        spooled to a temporary file first, so a whole season's track needn't fit in memory."""
        reader = await request.multipart()
        results = []
        while (part := await reader.next()) is not None:
            if part.name != "file":
                continue
            name = Path(part.filename or "upload").name
            with tempfile.TemporaryFile() as tmp:
                size = 0
                while chunk := await part.read_chunk(256 * 1024):
                    size += len(chunk)
                    if size > LOGBOOK_MAX_BYTES:
                        raise web.HTTPRequestEntityTooLarge(max_size=LOGBOOK_MAX_BYTES, actual_size=size)
                    tmp.write(chunk)
                tmp.seek(0)
                results.extend(await asyncio.to_thread(_read_logbook_file, self.fw.logbook, tmp, name))
        return web.json_response({"files": results})

    async def logbook_edit(self, request: web.Request) -> web.Response:
        body = await request.json()
        ok = await asyncio.to_thread(self.fw.logbook.set_info, request.match_info["trip_id"],
                                     body.get("name"), body.get("note"))
        if not ok:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def logbook_delete(self, request: web.Request) -> web.Response:
        if not await asyncio.to_thread(self.fw.logbook.delete, request.match_info["trip_id"]):
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def reserves(self, request: web.Request) -> web.Response:
        """Nature reserves touching a box (bbox=west,south,east,north), with their outlines."""
        try:
            west, south, east, north = (float(v) for v in request.query["bbox"].split(","))
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="bbox=west,south,east,north is needed") from None
        if not (south <= north and west <= east):
            raise web.HTTPBadRequest(text="bbox=west,south,east,north is needed")
        resp = web.json_response(self.fw.reserves.in_view(south, west, north, east))
        resp.enable_compression()  # outlines: a national park can be a few hundred kB
        return resp

    async def notify_picture(self, request: web.Request) -> web.StreamResponse:
        """A notification's map picture, standalone (as an add-on Home Assistant serves it)."""
        name = request.match_info["name"]
        path = config.MEDIA_DIR / name
        if not config.STANDALONE or not PICTURE_NAME.fullmatch(name) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "max-age=86400", "Content-Type": "image/png"})

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
