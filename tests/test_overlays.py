"""The weather overlay backbone: time lists, upstream addresses, the tile cache's own expiry,
points runs, and the endpoints through the real web app."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import ferrywatch.overlays as ovmod
import ferrywatch.web as webmod
from ferrywatch.overlays import (Overlay, Overlays, bbox_3857, every, parse_duration, parse_instant,
                                 parse_time_dimension, step_of, tile_bounds)
from ferrywatch.simoverlays import FakeTileSession, fake_wind_run, png, rain_tile
from ferrywatch.simoverlays import register as register_test_overlays
from ferrywatch.tilecache import TileCache

T0 = 1761400800  # 2025-10-25 14:00:00 UTC


# --- times -------------------------------------------------------------------------------

def test_iso_durations_and_instants():
    assert parse_duration("PT5M") == 300 and parse_duration("PT1H") == 3600 and parse_duration("P1D") == 86400
    assert parse_duration("P1DT6H") == 108000 and parse_duration("PT0.5S") == 0.5
    for bad in ("", "P", "5M", "PT"):
        with pytest.raises(ValueError):
            parse_duration(bad)
    assert parse_instant("2025-10-25T14:00:00Z") == T0
    assert parse_instant("2025-10-25T14:00:00.000Z") == T0
    assert parse_instant("2025-10-25T16:00:00+02:00") == T0
    assert parse_instant("2025-10-25T14:00:00") == T0  # no zone: UTC
    assert parse_instant("2025-10-25") == T0 - 14 * 3600


def test_time_dimensions_from_capabilities():
    # FMI's radar: an interval with a step.
    assert parse_time_dimension("2025-10-25T13:00:00Z/2025-10-25T14:00:00Z/PT5M") == [T0 - 3600 + i * 300 for i in range(13)]
    # A list of days, and both kinds mixed (duplicates once).
    days = "2025-10-23T00:00:00Z,2025-10-24T00:00:00Z, 2025-10-25T00:00:00Z"
    assert parse_time_dimension(days) == [T0 - 14 * 3600 - d * 86400 for d in (2, 1, 0)]
    mixed = "2025-10-25T13:50:00Z,2025-10-25T13:55:00Z/2025-10-25T14:00:00Z/PT5M"
    assert parse_time_dimension(mixed) == [T0 - 600, T0 - 300, T0]
    # A dimension spanning years keeps only the newest.
    years = parse_time_dimension("2015-01-01T00:00:00Z/2025-10-25T14:00:00Z/PT5M", limit=10)
    assert len(years) == 10 and years[-1] == T0 and years[0] == T0 - 9 * 300
    for bad in ("2025-10-25T14:00:00Z/PT5M", "2025-10-25T14:00Z/2025-10-25T13:00Z/PT5M", "soon"):
        with pytest.raises(ValueError):
            parse_time_dimension(bad)


def test_regular_times():
    radar = asyncio.run(every(300, 12, lag=300)(None, T0 + 7 * 60))  # 14:07: 14:00 is the newest 5 min old
    assert radar[-1] == T0 and radar[0] == T0 - 11 * 300 and len(radar) == 12
    forecast = asyncio.run(every(3600, 1, ahead=3)(None, T0 + 1800))
    assert forecast == [T0, T0 + 3600, T0 + 7200, T0 + 10800]
    assert step_of(radar) == 300 and step_of([T0]) is None and step_of([T0, T0 + 60, T0 + 3660, T0 + 7260, T0 + 10860]) == 3600


# --- addresses ---------------------------------------------------------------------------

def test_upstream_addresses():
    wmts = Overlay(id="ice", title="Ice", kind="tiles", attribution="", max_age=1,
                   url="https://x/wmts?layer=a&time={date}&TileMatrix={z}&TileCol={x}&TileRow={y}", times=every(86400, 1))
    assert wmts.tile_url(T0, 6, 36, 17) == "https://x/wmts?layer=a&time=2025-10-25&TileMatrix=6&TileCol=36&TileRow=17"
    wms = Overlay(id="radar", title="Radar", kind="tiles", attribution="", max_age=1,
                  url="https://x/wms?TIME={time}&BBOX={bbox}&ts={ts}", times=every(300, 12))
    assert wms.tile_url(T0, 0, 0, 0) == ("https://x/wms?TIME=2025-10-25T14:00:00Z"
                                         "&BBOX=-20037508.34,-20037508.34,20037508.34,20037508.34&ts=1761400800")
    minx, miny, maxx, maxy = bbox_3857(1, 1, 0)  # the north-east quarter
    assert minx == 0 and miny == 0 and maxx == pytest.approx(20037508.34) and maxy == pytest.approx(20037508.34)
    south, west, north, east = tile_bounds(1, 0, 1)
    assert (west, east) == (-180, 0) and north == 0 and south == pytest.approx(-85.0511, abs=1e-3)


# --- the registry ------------------------------------------------------------------------

class Counter:
    """A times function that counts its calls and can be told to fail."""

    def __init__(self, times):
        self.times, self.calls, self.fail = times, 0, None

    async def __call__(self, session, now):
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.times


def make(tmp_path, times, **kw):
    overlays = Overlays(tmp_path / "overlays", None, FakeTileSession())
    counter = Counter(times)
    overlays.register(Overlay(id="test-rain", title="Rain", kind="tiles", url="sim://test-rain/{ts}/{z}/{x}/{y}",
                              times=counter, attribution="Test", max_age=6 * 3600,
                              bounds=(58.5, 19.0, 61.5, 31.0), min_zoom=5, max_zoom=10, **kw))
    return overlays, counter


def test_registry_checks_overlays(tmp_path):
    overlays = Overlays(tmp_path, None)
    for bad in (dict(kind="tiles"), dict(kind="points"), dict(kind="lines", url="x", times=every(1, 1)),
                dict(kind="tiles", url="x", times=every(1, 1), id="a/b")):
        with pytest.raises(ValueError):
            overlays.register(Overlay(**{"id": "a", "title": "A", "attribution": "", "max_age": 1, **bad}))


def test_time_lists_are_reused_and_survive_a_failure(tmp_path, caplog):
    overlays, counter = make(tmp_path, [T0 - 300, T0], refresh=600)
    o = overlays.registry["test-rain"]

    async def run():
        assert await overlays.times(o, T0) == [T0 - 300, T0]
        assert await overlays.times(o, T0 + 599) == [T0 - 300, T0] and counter.calls == 1  # reused
        counter.fail = OSError("connect timeout")
        with caplog.at_level(logging.WARNING):
            assert await overlays.times(o, T0 + 600) == [T0 - 300, T0]  # the last good list
            assert overlays.error(o) == "connect timeout"
            # Tried again after RETRY, not on every request, and logged only once.
            assert await overlays.times(o, T0 + 610) == [T0 - 300, T0] and counter.calls == 2
            await overlays.times(o, T0 + 600 + ovmod.RETRY)
        assert counter.calls == 3
        assert sum("Could not read the Rain overlay" in r.message for r in caplog.records) == 1
        counter.fail = None
        counter.times = [T0, T0 + 300]
        assert await overlays.times(o, T0 + 2000) == [T0, T0 + 300] and overlays.error(o) is None

    asyncio.run(run())


def test_listing_only_asks_the_overlays_that_are_on(tmp_path):
    overlays, counter = make(tmp_path, [T0 - 300, T0])
    overlays.register(Overlay(id="winter", title="Ice", kind="tiles", url="x", times=every(86400, 1),
                              attribution="", max_age=1, season=lambda now: False))

    async def run():
        off = await overlays.listing(set(), T0)
        assert [o["id"] for o in off] == ["test-rain"]  # out of season: not listed
        assert off[0]["times"] is None and counter.calls == 0
        on = await overlays.listing({"test-rain"}, T0)
        assert on[0]["times"] == [T0 - 300, T0] and on[0]["valid_for"] == 300 and counter.calls == 1
        assert on[0]["bounds"] == (58.5, 19.0, 61.5, 31.0) and on[0]["error"] is None

    asyncio.run(run())


def test_tiles_only_for_listed_times_inside_the_overlay(tmp_path):
    overlays, counter = make(tmp_path, [T0 - 300, T0])

    async def run():
        path = await overlays.tile("test-rain", T0, 8, 145, 74)  # off Porkkala
        assert path and path.read_bytes().startswith(b"\x89PNG")
        assert path == tmp_path / "overlays" / "tiles" / "test-rain" / str(T0) / "8" / "145" / "74.png"
        assert await overlays.tile("test-rain", T0 + 1, 8, 145, 74) is None  # a time it doesn't have
        assert await overlays.tile("test-rain", T0, 8, 10, 10) is None  # far outside its bounds
        assert await overlays.tile("test-rain", T0, 11, 1160, 593) is None  # deeper than max_zoom
        assert await overlays.tile("test-rain", T0, 4, 9, 4) is None  # shallower than min_zoom
        assert await overlays.tile("nothing", T0, 8, 145, 74) is None

    asyncio.run(run())


def test_points_runs_are_kept_on_disk(tmp_path):
    calls = []

    async def wind(session, now):
        calls.append(now)
        if len(calls) == 2:
            raise ValueError("bad run")
        return await fake_wind_run(session, now)

    overlays = Overlays(tmp_path / "overlays", None)
    overlays.register(Overlay(id="wind", title="Wind", kind="points", points=wind, attribution="", max_age=3 * 3600))
    o = overlays.registry["wind"]

    async def run():
        times = await overlays.times(o, T0)
        assert len(times) == 48 and times[0] == T0 + 3600
        frame = await overlays.points("wind", times[3], T0)
        assert frame["fields"] == ["ms", "gust", "dir"] and len(frame["points"]) == 120
        assert len(frame["points"][0]) == 5 and frame["time"] == times[3]
        assert await overlays.points("wind", T0 + 1, T0) is None
        assert (tmp_path / "overlays" / "points" / "wind.json").exists() and len(calls) == 1
        overlays._runs.clear()  # as after RUN_KEPT: read back from the disk, not fetched
        assert await overlays.times(o, T0 + 60) == times and len(calls) == 1
        # An old run is fetched again; a failure keeps the old one and shows the error.
        assert await overlays.times(o, T0 + 4 * 3600) == times and len(calls) == 2
        assert overlays.error(o) == "bad run"
        assert await overlays.times(o, T0 + 4 * 3600 + 10) == times and len(calls) == 2  # not retried at once
        later = await overlays.times(o, T0 + 4 * 3600 + ovmod.RETRY)
        assert len(calls) == 3 and later[0] == T0 + 5 * 3600 and overlays.error(o) is None

    asyncio.run(run())


# --- the tile cache's own expiry ---------------------------------------------------------

def test_cache_ages_per_source_and_prunes_expired_frames(tmp_path):
    cache = TileCache(tmp_path, FakeTileSession(), max_bytes=10**6)
    cache.max_ages["test-rain"] = 6 * 3600
    now = time.time()

    async def run():
        return await cache.get(f"test-rain/{T0}", 8, 145, 74, f"sim://test-rain/{T0}/8/145/74")

    frame = asyncio.run(run())
    chart = cache.path("chart", 8, 145, 74)
    chart.parent.mkdir(parents=True)
    chart.write_bytes(b"PNG")
    for p in (frame, chart):
        os.utime(p, (now - 7 * 3600, now - 7 * 3600))  # 7 hours old
    assert cache.max_age(f"test-rain/{T0}") == 6 * 3600 and cache.max_age("chart") == 30 * 86400
    assert cache.prune(now) == 1
    assert not frame.exists() and not (tmp_path / "test-rain").exists()  # its folders went too
    assert chart.exists()  # a chart tile is kept as long as the size allows


def test_cache_refetches_an_expired_frame_and_tracks_failures(tmp_path):
    class Failing:
        def get(self, url, **kw):
            import aiohttp
            raise aiohttp.ClientError("down")

    cache = TileCache(tmp_path, FakeTileSession())
    cache.max_ages["test-rain"] = 60
    source, url = f"test-rain/{T0}", f"sim://test-rain/{T0}/8/145/74"

    async def run():
        path = await cache.get(source, 8, 145, 74, url)
        os.utime(path, (time.time() - 120, time.time() - 120))
        cache.session = Failing()
        again = await cache.get(source, 8, 145, 74, url)  # too old: asked again, fails, old copy served
        assert again == path and cache.failing == {"test-rain": "down"}
        cache.session = FakeTileSession()
        assert await cache.get(source, 8, 145, 74, url) == path and cache.failing == {}
        assert time.time() - path.stat().st_mtime < 5  # fetched afresh

    asyncio.run(run())


# --- the simulator's test overlays -------------------------------------------------------

def test_test_overlays_draw_and_move():
    assert png(1, 1, b"\x01\x02\x03\x04").startswith(b"\x89PNG\r\n\x1a\n")
    a, b = rain_tile(T0, 8, 145, 74), rain_tile(T0 + 1800, 8, 145, 74)
    assert a.startswith(b"\x89PNG") and a != b  # the cells drift between frames


# --- endpoints ---------------------------------------------------------------------------

def test_endpoints(tmp_path, monkeypatch):
    async def main():
        overlays = Overlays(tmp_path / "overlays", None, FakeTileSession())
        register_test_overlays(overlays)
        client = TestClient(TestServer(webmod.Web(SimpleNamespace(overlays=overlays)).app))
        await client.start_server()
        try:
            listing = await (await client.get("/api/overlays")).json()
            assert [o["id"] for o in listing["overlays"]] == ["test-wind", "sea-ice", "rain-radar"]
            assert all(o["times"] is None for o in listing["overlays"]) and listing["now"] > 0
            listing = await (await client.get("/api/overlays?times=rain-radar,test-wind")).json()
            wind, rain = (next(o for o in listing["overlays"] if o["id"] == i) for i in ("test-wind", "rain-radar"))
            assert len(rain["times"]) == 12 and len(wind["times"]) == 48 and rain["frame_ms"] == 500
            r = await client.get(f"/api/overlay/rain-radar/{rain['times'][-1]}/8/145/74.png")
            assert r.status == 200 and r.headers["Content-Type"] == "image/png"
            assert r.headers["Cache-Control"] == f"max-age={6 * 3600}"
            assert (await r.read()).startswith(b"\x89PNG")
            assert (await client.get(f"/api/overlay/rain-radar/{rain['times'][-1] + 1}/8/145/74.png")).status == 404
            assert (await client.get("/api/overlay/rain-radar/soon/8/145/74.png")).status == 400
            assert (await client.get("/api/overlay/none/1/8/145/74.png")).status == 404
            frame = await (await client.get(f"/api/overlay/test-wind/{wind['times'][0]}")).json()
            assert frame["fields"] == ["ms", "gust", "dir"] and len(frame["points"]) == 120
            assert (await client.get("/api/overlay/rain-radar/1")).status == 404  # tiles, not points
        finally:
            await client.close()

    asyncio.run(main())


# --- the panel's time maths (static/overlays.js, under Node) -------------------------------

OVERLAYS_JS = Path(__file__).resolve().parents[1] / "ferry_watch" / "app" / "ferrywatch" / "static" / "overlays.js"
NODE = shutil.which("node")

SCRIPT = """
const O = require(process.argv[1]);
const radar = [0, 300, 600, 900], wind = [1200, 4800, 8400];
console.log(JSON.stringify({
  union: O.union([radar, wind, [600]]),
  radarAt: [O.frameAt(radar, 650, 300), O.frameAt(radar, 1250, 300), O.frameAt(radar, -100, 300), O.frameAt(radar, -400, 300)],
  windAt: [O.frameAt(wind, 900, 3600), O.frameAt(wind, 5000, 3600), O.frameAt(wind, 20000, 3600)],
  ice: O.frameAt([86400], 86400 + 50000, 3 * 86400),
  index: [O.indexAt(radar, 650), O.indexAt(radar, -5), O.indexAt(radar, 5000)],
  start: [O.startTime(radar, 700), O.startTime(wind, 700), O.startTime([], 0)],
  rel: [O.relative(-30), O.relative(-25 * 60), O.relative(3 * 3600), O.relative(-3 * 86400)],
}));
"""


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_time_bar_maths():
    out = json.loads(subprocess.run([NODE, "-e", SCRIPT, str(OVERLAYS_JS)], capture_output=True, text=True,
                                    check=True).stdout)
    assert out["union"] == [0, 300, 600, 900, 1200, 4800, 8400]
    # Radar: the frame at or before; nothing once a step past the last; the first just before it.
    assert out["radarAt"] == [600, None, 0, None]
    # Wind: the next forecast hour just before the first; nothing long after the last.
    assert out["windAt"] == [1200, 4800, None]
    assert out["ice"] == 86400  # a daily chart stays up for its valid_for
    assert out["index"] == [2, 0, 3]
    assert out["start"] == [600, 1200, None]
    assert out["rel"] == ["now", "25 min ago", "in 3 h", "3 days ago"]
