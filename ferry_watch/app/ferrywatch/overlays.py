"""Weather layers over the map: one registry that the ice chart, rain radar and wind plug into.

Each overlay says where its pictures or numbers come from and lists the times it has
(radar frames, forecast hours, ice chart days); the panel picks a time with its shared time
bar and asks for that frame. Two kinds:

- "tiles": map tiles from an upstream WMS or WMTS, fetched through a TileCache of the
  overlays' own, so frames expire on the overlay's own schedule (radar in hours, not the
  chart's month) and a size limit of their own keeps them from crowding out chart tiles.
- "points": numbers on a grid of points (a wind forecast), fetched as one run to a small
  file on disk and handed to the panel one time at a time as compact JSON.

Nothing is fetched until a panel asks. A source that is down is logged once, shown with
its switch and in Settings, and never stops the rest of the app.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from .tilecache import TileCache

_LOGGER = logging.getLogger(__name__)

MAX_BYTES = 80 * 1024 * 1024  # overlay tiles on disk, apart from the chart's 300 MB
MAX_TIMES = 500  # times listed per overlay (the newest)
RETRY = 120  # s before a failed time list or points run is asked for again
PRUNE_EVERY = 1800  # s between removals of expired frames while overlays are in use
RUN_KEPT = 60  # s a points run stays parsed in memory after it was last asked for

TimesFn = Callable[[aiohttp.ClientSession | None, float], Awaitable[list[float]]]
PointsFn = Callable[[aiohttp.ClientSession | None, float], Awaitable[dict[str, Any]]]


@dataclass
class Overlay:
    """One weather layer. `url` is the upstream address of one tile, with any of {z} {x} {y},
    {bbox} (the tile's EPSG:3857 box, for WMS), {time} (ISO 8601 in UTC), {date} (YYYY-MM-DD)
    and {ts} (Unix seconds). `times` lists what the source has; `points` (points overlays)
    fetches a whole run: {"times": [ts], "fields": [name], "points": [[lat, lon]],
    "values": {"<ts>": [[value per field] per point]}}, whose times are then the list."""

    id: str
    title: str
    kind: str  # "tiles" or "points"
    attribution: str
    max_age: float  # s a fetched frame (or points run) is kept before it's fetched again
    url: str = ""
    times: TimesFn | None = None
    points: PointsFn | None = None
    note: str = ""  # one line under its switch
    color: str = "#0ea5e9"  # the switch row's dot
    opacity: float = 0.7  # until the viewer picks their own
    min_zoom: int = 3
    max_zoom: int = 10  # the deepest zoom fetched; closer in, the panel enlarges these
    tile_size: int = 256
    bounds: tuple[float, float, float, float] | None = None  # south, west, north, east
    valid_for: float | None = None  # s a frame stays up after its time (default: the step)
    frame_ms: int = 500  # time bar speed while playing
    refresh: float = 600  # s a time list is reused
    season: Callable[[float], bool] | None = None  # listed only while this says so

    def tile_url(self, ts: int, z: int, x: int, y: int) -> str:
        values = {"z": z, "x": x, "y": y, "ts": ts, "time": iso(ts), "date": iso(ts)[:10]}
        if "{bbox}" in self.url:
            values["bbox"] = ",".join(f"{v:.2f}" for v in bbox_3857(z, x, y))
        return self.url.format(**values)


# --- times ------------------------------------------------------------------------------

def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def every(step: int, count: int, lag: float = 0, ahead: int = 0) -> TimesFn:
    """Times on a regular step, for sources that publish on a clock: the newest `count`
    at least `lag` seconds old (a radar frame takes a few minutes to appear), and `ahead`
    more after now (forecast hours). every(300, 12, lag=300) is the last hour of 5-minute
    radar frames."""
    async def times(session: aiohttp.ClientSession | None, now: float) -> list[float]:
        last = int((now - lag) // step * step)
        return [last - i * step for i in range(count - 1, -1, -1)] + [last + i * step for i in range(1, ahead + 1)]
    return times


_DURATION = re.compile(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def parse_duration(text: str) -> float:
    """Seconds in an ISO 8601 duration such as PT5M, PT1H or P1D."""
    m = _DURATION.match(text.strip())
    if not m or not any(m.groups()):
        raise ValueError(f"not a duration: {text!r}")
    w, d, h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return (((w * 7 + d) * 24 + h) * 60 + mi) * 60 + s


def parse_instant(text: str) -> int:
    """Unix seconds of an ISO 8601 time; a time without a zone is UTC."""
    t = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int(t.timestamp())


def parse_time_dimension(text: str, limit: int = MAX_TIMES) -> list[int]:
    """Times in a WMS or WMTS time dimension, as GetCapabilities gives them: a list of
    instants, start/end/step intervals, or both, comma separated. The newest `limit`."""
    times: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split("/")
        if len(bits) == 1:
            times.add(parse_instant(bits[0]))
            continue
        if len(bits) != 3:
            raise ValueError(f"not a time interval: {part!r}")
        start, end, step = parse_instant(bits[0]), parse_instant(bits[1]), parse_duration(bits[2])
        if step <= 0 or end < start:
            raise ValueError(f"not a time interval: {part!r}")
        n = int((end - start) // step)
        first = max(0, n + 1 - limit)  # only the newest; a dimension can span years
        times.update(int(start + i * step) for i in range(first, n + 1))
    return sorted(times)[-limit:]


def step_of(times: list[int]) -> float | None:
    """The usual gap between an overlay's times (the median), None with fewer than two."""
    gaps = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
    return float(gaps[len(gaps) // 2]) if gaps else None


# --- tiles ------------------------------------------------------------------------------

HALF_WORLD = 20037508.342789244  # m, web mercator


def bbox_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """A tile's box in web mercator metres: min x, min y, max x, max y."""
    size = 2 * HALF_WORLD / 2**z
    return (-HALF_WORLD + x * size, HALF_WORLD - (y + 1) * size, -HALF_WORLD + (x + 1) * size, HALF_WORLD - y * size)


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """A tile's south, west, north, east in degrees."""
    def lat(row: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / 2**z))))
    return lat(y + 1), x / 2**z * 360 - 180, lat(y), (x + 1) / 2**z * 360 - 180


def overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# --- the registry -----------------------------------------------------------------------

class Overlays:
    def __init__(self, directory: Path, session: aiohttp.ClientSession | None, tile_session: Any = None):
        self.dir = directory
        self.session = session
        # The simulator hands in a session that draws its own test tiles.
        self.cache = TileCache(directory / "tiles", tile_session or session, max_bytes=MAX_BYTES)
        self.registry: dict[str, Overlay] = {}
        self._times: dict[str, tuple[float, list[int]]] = {}  # id -> (when listed, times)
        self._errors: dict[str, str] = {}  # time lists and points runs that failed last time
        self._logged: set[str] = set()  # overlays whose failure is in the log already
        self._runs: dict[str, dict[str, Any]] = {}  # points runs, parsed, kept briefly
        self._run_tried: dict[str, float] = {}
        self._drop: dict[str, asyncio.TimerHandle] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pruned = time.time()  # housekeeping prunes at start; this is for in between
        self._prune_task: asyncio.Task | None = None

    def register(self, overlay: Overlay) -> None:
        if overlay.kind == "tiles" and (not overlay.url or not overlay.times):
            raise ValueError(f"{overlay.id}: a tiles overlay needs url and times")
        if overlay.kind == "points" and not overlay.points:
            raise ValueError(f"{overlay.id}: a points overlay needs points")
        if overlay.kind not in ("tiles", "points") or "/" in overlay.id:
            raise ValueError(f"{overlay.id}: bad kind or id")
        self.registry[overlay.id] = overlay
        if overlay.kind == "tiles":
            self.cache.max_ages[overlay.id] = overlay.max_age

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def error(self, overlay: Overlay) -> str | None:
        return self._errors.get(overlay.id) or self.cache.failing.get(overlay.id)

    def _note(self, overlay: Overlay) -> None:
        """Log a failure once, when it starts, and the recovery."""
        err = self.error(overlay)
        if err and overlay.id not in self._logged:
            self._logged.add(overlay.id)
            _LOGGER.warning("Could not read the %s overlay: %s", overlay.title, err)
        elif not err and overlay.id in self._logged:
            self._logged.discard(overlay.id)
            _LOGGER.info("The %s overlay works again", overlay.title)

    # --- times ---

    async def times(self, overlay: Overlay, now: float | None = None) -> list[int]:
        """The times the overlay has now; the last good list while its source is down."""
        now = now or time.time()
        if overlay.kind == "points":
            run = await self._run(overlay, now)
            return run["times"] if run else []
        cached = self._times.get(overlay.id)
        if cached and now - cached[0] < overlay.refresh:
            return cached[1]
        async with self._lock(overlay.id):
            cached = self._times.get(overlay.id)
            if cached and now - cached[0] < overlay.refresh:
                return cached[1]
            old = cached[1] if cached else []
            try:
                listed = await overlay.times(self.session, now)
                times = sorted({int(t) for t in listed})[-MAX_TIMES:]
            except Exception as err:  # noqa: BLE001 - a broken source never stops the rest
                self._errors[overlay.id] = str(err) or type(err).__name__
                self._times[overlay.id] = (now - overlay.refresh + RETRY, old)
                self._note(overlay)
                return old
            self._errors.pop(overlay.id, None)
            self._times[overlay.id] = (now, times)
            self._note(overlay)
            return times

    async def listing(self, with_times: set[str], now: float | None = None) -> list[dict[str, Any]]:
        """Every overlay in season for the panel, with the times of those in `with_times`
        (the ones switched on: the others' sources aren't asked)."""
        now = now or time.time()
        shown = [o for o in self.registry.values() if not o.season or o.season(now)]

        async def times(o: Overlay) -> list[int] | None:
            return await self.times(o, now) if o.id in with_times else None

        all_times = await asyncio.gather(*(times(o) for o in shown))
        out = []
        for o, times in zip(shown, all_times):
            out.append({
                "id": o.id, "title": o.title, "kind": o.kind, "note": o.note, "color": o.color,
                "attribution": o.attribution, "opacity": o.opacity, "min_zoom": o.min_zoom,
                "max_zoom": o.max_zoom, "tile_size": o.tile_size, "bounds": o.bounds,
                "valid_for": o.valid_for or step_of(times or []) or 86400, "frame_ms": o.frame_ms,
                "refresh": o.refresh, "times": times, "error": self.error(o),
            })
        return out

    # --- tiles ---

    async def tile(self, overlay_id: str, ts: int, z: int, x: int, y: int) -> Path | None:
        """One tile of one frame, from the disk or fetched; None where there is none."""
        o = self.registry.get(overlay_id)
        if not o or o.kind != "tiles" or not (o.min_zoom <= z <= o.max_zoom and 0 <= x < 2**z and 0 <= y < 2**z):
            return None
        if o.bounds and not overlaps(o.bounds, tile_bounds(z, x, y)):
            return None
        if ts not in await self.times(o):  # only times the source has: nothing else is fetched or kept
            return None
        path = await self.cache.get(f"{o.id}/{ts}", z, x, y, o.tile_url(ts, z, x, y))
        self._note(o)
        self._maybe_prune()
        return path

    def _maybe_prune(self) -> None:
        # Radar frames should leave the disk within hours; housekeeping only runs every 6.
        now = time.time()
        if now - self._pruned >= PRUNE_EVERY and not (self._prune_task and not self._prune_task.done()):
            self._pruned = now
            self._prune_task = asyncio.create_task(asyncio.to_thread(self.prune))

    def prune(self) -> int:
        removed = self.cache.prune()
        if removed:
            _LOGGER.debug("Removed %s old overlay tiles", removed)
        return removed

    # --- points ---

    def _run_path(self, overlay: Overlay) -> Path:
        return self.dir / "points" / f"{overlay.id}.json"

    async def _run(self, overlay: Overlay, now: float) -> dict[str, Any] | None:
        """The overlay's latest run: from memory, the disk, or fetched when old."""
        run = self._runs.get(overlay.id)
        if run and now - run["fetched"] < overlay.max_age:
            self._keep(overlay.id)
            return run
        async with self._lock(overlay.id):
            path = self._run_path(overlay)
            run = self._runs.get(overlay.id) or await asyncio.to_thread(_read_run, path)
            if (not run or now - run["fetched"] >= overlay.max_age) and now - self._run_tried.get(overlay.id, 0) >= RETRY:
                self._run_tried[overlay.id] = now
                try:
                    fresh = _check_run(await overlay.points(self.session, now))
                except Exception as err:  # noqa: BLE001 - a broken source never stops the rest
                    self._errors[overlay.id] = str(err) or type(err).__name__
                else:
                    self._errors.pop(overlay.id, None)
                    run = {**fresh, "fetched": now}
                    await asyncio.to_thread(_write_run, path, run)
                self._note(overlay)
            if run:
                self._runs[overlay.id] = run
                self._keep(overlay.id)
            return run

    def _keep(self, overlay_id: str) -> None:
        """Keep a parsed run for RUN_KEPT seconds after it was last used, then let it go."""
        handle = self._drop.pop(overlay_id, None)
        if handle:
            handle.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._drop[overlay_id] = loop.call_later(RUN_KEPT, self._runs.pop, overlay_id, None)

    async def points(self, overlay_id: str, ts: int, now: float | None = None) -> dict[str, Any] | None:
        """One time of a points overlay: {"time", "fields", "points": [[lat, lon, *values]]}."""
        o = self.registry.get(overlay_id)
        if not o or o.kind != "points":
            return None
        run = await self._run(o, now or time.time())
        values = run["values"].get(str(ts)) if run else None
        if values is None:
            return None
        return {"time": ts, "fields": run["fields"],
                "points": [[*p, *v] for p, v in zip(run["points"], values)]}


def _check_run(run: dict[str, Any]) -> dict[str, Any]:
    """A points run in the shape the panel expects, or ValueError."""
    times = sorted(int(t) for t in run["times"])
    fields = [str(f) for f in run["fields"]]
    points = [[round(float(lat), 4), round(float(lon), 4)] for lat, lon in run["points"]]
    values = {}
    for t in times:
        rows = run["values"][str(t)]
        if len(rows) != len(points) or any(len(r) != len(fields) for r in rows):
            raise ValueError(f"run values at {t} don't match its points and fields")
        values[str(t)] = [[None if v is None else round(float(v), 1) for v in r] for r in rows]
    return {"times": times, "fields": fields, "points": points, "values": values}


def _read_run(path: Path) -> dict[str, Any] | None:
    try:
        run = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return run if isinstance(run, dict) and "fetched" in run else None


def _write_run(path: Path, run: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(run, separators=(",", ":")))
    os.replace(tmp, path)
