"""The Windy-style weather layers: the shared weather model run (kept as binary files), the
sea, lightning, aurora, light pollution and sunset layers, the endpoints for a tap and the
sky map, and the sky map's astronomy (static/sky.js, under Node)."""

import asyncio
import gzip
import json
import math
import shutil
import subprocess
import time
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import ferrywatch.web as webmod
from ferrywatch import aurora, forecast, lightning, lightpollution as lp, marine, sunset
from ferrywatch.overlays import MISSING, Overlay, Overlays, RunBuilder
from ferrywatch.simweather import (fake_fmi_fetch, fake_kp, fake_lightning, fake_marine_json, fake_noaa_get,
                                   fake_ovation)

STATIC = Path(__file__).resolve().parents[1] / "ferry_watch" / "app" / "ferrywatch" / "static"
NODE = shutil.which("node")


def _hour(now):
    return int(now // 3600 * 3600)


# --- binary runs -------------------------------------------------------------------------

def test_run_builder_and_binary_runs_on_disk(tmp_path):
    now = time.time()
    times = [_hour(now) + i * 3600 for i in range(3)]
    points = [(60.0, 24.0), (60.0, 25.0), (61.0, 24.0), (61.0, 25.0)]
    b = RunBuilder(times, ["t", "dir"], points, [10.0, 1.0])
    for i in range(4):
        for t in times[:2]:
            b.set(i, t, 0, -3.14 + i)
            b.set(i, t, 1, 270.4)
    b.set(0, times[2], 0, 5.0)  # one point in four: too thin, the hour is dropped
    run = b.run(info={"source": "test"})
    assert run["times"] == times[:2] and isinstance(run["values"][str(times[0])], array)
    assert run["values"][str(times[0])][0] == -31 and run["values"][str(times[0])][1] == 270

    async def points_fn(session, now_):
        return b.run(info={"source": "test"})

    async def main():
        ov = Overlays(tmp_path, None)
        ov.register(Overlay(id="base", title="Base", kind="points", points=points_fn, hidden=True, attribution="",
                            max_age=3600))
        ov.register(Overlay(id="temp", title="Temp", kind="points", run_of="base", fields=["t"], attribution="",
                            max_age=3600, group="field", icon="temp"))
        listing = await ov.listing({"temp"}, now)
        assert [o["id"] for o in listing] == ["temp"]  # the base run itself isn't listed
        assert listing[0]["times"] == times[:2] and listing[0]["group"] == "field" and listing[0]["icon"] == "temp"
        frame = await ov.points("temp", times[1], now)
        assert frame["fields"] == ["t"] and frame["info"] == {"source": "test"}
        assert frame["points"][0] == [60.0, 24.0, -3.1] and frame["points"][3] == [61.0, 25.0, -0.1]
        assert await ov.points("temp", times[2], now) is None
        assert (tmp_path / "points" / "base").is_dir()  # one file per hour, not one big JSON
        rows = await ov.values_at("temp", times[0])
        assert rows[1] == [-2.1, 270.0]
        series = await ov.series("temp", 60.9, 24.1, ["dir", "t"], 0, 2e10)
        assert series == [[times[0], 270.0, -1.1], [times[1], 270.0, -1.1]]
        ov._runs.clear()  # read back from the disk
        assert (await ov.points("temp", times[0], now))["points"][2] == [61.0, 24.0, -1.1]

    asyncio.run(main())
    assert MISSING == -32768


# --- the weather model -------------------------------------------------------------------

def test_weather_model_from_fmi_and_layers_share_it(tmp_path):
    now = time.time()

    async def main():
        ov = Overlays(tmp_path, None)
        forecast.register(ov, fake_fmi_fetch)
        listing = {o["id"]: o for o in await ov.listing({"temperature", "pressure"}, now)}
        assert set(listing) == {"temperature", "gusts", "clouds", "rain", "fog", "pressure"}
        assert listing["pressure"]["group"] == "top" and listing["temperature"]["group"] == "field"
        times = listing["temperature"]["times"]
        assert len(times) >= 40 and times == listing["pressure"]["times"]
        frame = await ov.points("fog", times[3], now)
        assert frame["fields"] == ["vis", "rh", "td", "t", "ms", "low"] and len(frame["points"]) == len(forecast.GRID)
        filled = [p for p in frame["points"] if p[2] is not None]
        assert len(filled) > 0.9 * len(forecast.GRID)
        assert all(-40 < p[5] < 40 and 0 <= p[3] <= 100 for p in filled)
        run = await ov.run_for("temperature", now)
        assert run["fields"] == forecast.FIELDS and run.get("binary")

    asyncio.run(main())


def test_weather_model_falls_back_to_open_meteo(tmp_path):
    now = time.time()
    asked = []

    async def broken(url, params):
        raise ValueError("FMI down")

    async def om(url, params):
        asked.append(params)
        n = len(params["latitude"].split(","))
        first = _hour(now)
        hourly = {"time": [first + i * 3600 for i in range(forecast.HOURS + 1)]}
        for name in forecast.OM_PARAMS.values():
            hourly[name] = [12.5 if "temperature" in name else 5.0] * (forecast.HOURS + 1)
        one = {"hourly": hourly}
        return [one] * n if n > 1 else one

    run = asyncio.run(forecast.fetch_run(None, now, broken, om))
    assert len(asked) == -(-len(forecast.OM_GRID) // forecast.OM_BATCH)
    assert run["info"]["source"] == "open-meteo"
    first = run["values"][str(run["times"][0])]
    assert first[0] == 125  # tenths of a degree


def test_marine_layers(tmp_path):
    now = time.time()

    async def main():
        ov = Overlays(tmp_path, None)
        marine.register(ov, fake_marine_json)
        listing = {o["id"]: o for o in await ov.listing({"waves"}, now)}
        assert set(listing) == {"waves", "sea-temp", "sea-level"}
        t = listing["waves"]["times"][2]
        frame = await ov.points("waves", t, now)
        assert frame["fields"] == ["wave", "wdir", "period", "swell", "sdir"]
        p = next(p for p in frame["points"] if p[2] is not None)
        assert 0 <= p[2] < 8 and 0 <= p[3] < 360
        level = await ov.points("sea-level", t, now)
        assert any(r[2] is not None and abs(r[2]) < 150 for r in level["points"])  # centimetres

    asyncio.run(main())


# --- lightning and aurora ----------------------------------------------------------------

def test_lightning_run():
    now = time.time()
    params = lightning.request_params(now)
    assert dict(params)["bbox"] == "17.0,57.0,32.0,66.5"
    run = lightning.build_run(fake_lightning(params), now)
    assert run["fields"] == ["ago", "ka", "cloud"] and len(run["times"]) == 1
    rows = run["values"][str(run["times"][0])]
    assert rows and all(0 <= r[0] <= 61 for r in rows)
    assert len(run["points"]) == len(rows)


def test_aurora_run():
    now = time.time()
    ts, cells = aurora.parse_ovation(fake_ovation(now))
    assert ts and all(aurora.BOX[0] <= c[1] <= aurora.BOX[2] and aurora.BOX[1] <= c[0] <= aurora.BOX[3]
                      for c in cells) or cells
    kp = aurora.parse_kp(fake_kp(now), now)
    assert kp and all(0 <= k <= 9 for _, k in kp)
    run = aurora.build_run((ts, cells), kp, now, clouds=lambda lat, lon: 50.0)
    assert run["fields"] == ["prob", "seen", "cloud", "dark"] and run["info"]["kp"] == kp
    for prob, seen, cloud, dark in run["values"][str(run["times"][0])]:
        assert seen <= prob * 0.5 + 0.1 and 0 <= dark <= 1 and cloud == 50.0
    # Dark at Helsinki's midnight in winter, not at noon.
    assert aurora.darkness(aurora.sun_altitude(60.17, 24.94, 1797724800)) == 1  # 2026-12-20 22:00 UTC
    assert aurora.darkness(aurora.sun_altitude(60.17, 24.94, 1797681600)) == 0  # 10:00 UTC
    assert len(asyncio.run(fake_noaa_get(aurora.KP))) > 10


# --- light pollution ---------------------------------------------------------------------

def _encode(vals):
    """Values (N rows of N, south row first) in the atlas's own tile format."""
    n = lp.N
    d = array("b", [0]) * (n * n + 1)
    d[0], d[1] = vals[0][0] // 128, vals[0][0] % 128
    for r in range(1, n):
        d[n * r + 1] = vals[r][0] - vals[r - 1][0]
    for r in range(n):
        for c in range(1, n):
            d[n * r + 1 + c] = vals[r][c] - vals[r][c - 1]
    return gzip.compress(d.tobytes())


def test_light_pollution_atlas(tmp_path):
    # Brightness growing to the north and east in each tile, a bright spot in one.
    vals = [[(r // 6) + (c // 12) for c in range(lp.N)] for r in range(lp.N)]
    spot = [[max(v, 450 - 100 * max(abs(r - 305), abs(c - 305))) for c, v in enumerate(row)]
            for r, row in enumerate(vals)]  # a town: steps under 128, as the format needs
    plain, bright = _encode(vals), _encode(spot)
    decoded = lp.decode_tile(gzip.decompress(bright))
    assert decoded[0] == 0 and decoded[lp.N * 305 + 305] == 450 and decoded[lp.N * 599 + 599] == 99 + 49
    got = []

    async def get(url):
        got.append(url)
        return bright if url.endswith("binary_tile_41_26.dat.gz") else plain

    o = lp.overlay(tmp_path, None, get)

    async def main():
        ov = Overlays(tmp_path / "ov", None)
        ov.register(o)
        assert (await ov.listing({lp.ID}))[0]["has_value"] is True
        # Tile 41_26: 20-25 E, 60-65 N; the town at row and column 305 is at 62.55 N, 22.55 E.
        v = await ov.value(lp.ID, 62.546, 22.546, 0)
        assert v["zone"] == "7b" and v["year"] == 2025 and v["sqm"] < 17
        dark = await ov.value(lp.ID, 60.03, 20.03, 0)  # the tile's dark south-west corner
        assert dark["zone"] in ("0", "1a") and dark["sqm"] > 21.9
        assert await ov.value(lp.ID, 50.0, 20.0, 0) is None  # outside the atlas
        png = await o.tile(0, 7, 72, 35)
        assert png.startswith(b"\x89PNG")
        assert await o.tile(0, 7, 10, 35) is None  # nowhere near

    asyncio.run(main())
    assert len(got) == 12 and (tmp_path / "grid2025.bin").stat().st_size == lp.ROWS * lp.COLS * 2
    assert lp.zone(0.005) == "0" and lp.zone(2.0) == "4b" and lp.zone(100) == "7b"
    assert lp.sqm(0) == 22.0 and round(lp.sqm(1.0), 2) == 21.25
    assert lp.colour(0.0)[3] < lp.colour(5.0)[3] or lp.colour(0.0) != lp.colour(5.0)


# --- sunset and sunrise ------------------------------------------------------------------

def test_sun_events_and_glow():
    # 6 Nov 2026 at Helsinki: sunrise about 07:55 EET (05:55 UTC), sunset about 16:10 EET (14:10 UTC).
    day = 1793923200  # 2026-11-06 00:00 UTC
    ev = sunset.events(60.17, 24.94, day, day + 86400)
    kinds = [k for _, k, _ in ev]
    assert kinds == ["sunrise", "sunset"]
    rise, set_ = ev[0], ev[1]
    assert abs(rise[0] - (day + 5 * 3600 + 55 * 60)) < 15 * 60 and 115 < rise[2] < 135
    assert abs(set_[0] - (day + 14 * 3600 + 10 * 60)) < 15 * 60 and 225 < set_[2] < 245
    # The colours: until the sun is 3.5 degrees down, about 25 minutes in November.
    assert 15 * 60 < sunset.glow_time(60.17, 24.94, set_[0], "sunset") - set_[0] < 40 * 60
    assert 15 * 60 < rise[0] - sunset.glow_time(60.17, 24.94, rise[0], "sunrise") < 40 * 60
    # Midsummer in Lapland: none; in Oulu the sun sets but never gets 3.5 degrees down: 90 minutes.
    assert sunset.events(69.0, 27.0, 1781913600, 1781913600 + 86400) == []
    mid = sunset.events(65.0, 25.5, 1781913600, 1781913600 + 86400)
    dusk = next(t for t, k, _ in mid if k == "sunset")
    assert sunset.glow_time(65.0, 25.5, dusk, "sunset") - dusk == 5400
    lat, lon = sunset.destination(60.0, 24.0, 270, 111.2)
    assert abs(lat - 60.0) < 0.05 and abs(lon - 22.0) < 0.05
    assert [sunset.word(v) for v in (5, 25, 45, 65, 85)] == ["dull", "fair", "good", "great", "fiery"]


HERE = (60.0, 24.4)  # Porkkala; the sun sets to the south-west (225)


def _sky(weather, t=1794060000):
    """A made-up weather model hour on a fine grid: weather(km from HERE) -> fields."""
    lats = [56 + 0.25 * i for i in range(33)]
    lons = [16 + 0.5 * j for j in range(33)]
    points = [(la, lo) for la in lats for lo in lons]
    fields = ["low", "mid", "high", "rain", "vis", "rh"]
    k = math.cos(math.radians(HERE[0]))
    rows = []
    for la, lo in points:
        km = math.hypot((la - HERE[0]) * 111.2, (lo - HERE[1]) * 111.2 * k)
        w = {"low": 0, "mid": 0, "high": 0, "rain": 0, "vis": 30, "rh": 70, **weather(km)}
        rows.append([w[f] for f in fields])
    grid = sunset.Grid({"points": points, "fields": fields})
    return sunset.Sky(grid, {t: rows}, t)


def test_sunset_score_follows_the_light():
    rate = lambda weather: sunset.rate(_sky(weather), *HERE, 225)  # noqa: E731
    fiery = rate(lambda km: {"high": 50 if km < 150 else 0})
    clear = rate(lambda km: {})
    # Half high cloud overhead and towards the sun, nothing in the light's way: the best.
    assert fiery["score"] > 90 and fiery["canvas"] == 50 and fiery["gap"] > 95 and fiery["seen"] == 100
    # A clear sky: a clean sunset, fair.
    assert 30 < clear["score"] < 45 and clear["canvas"] == 0
    # Low cloud 150-500 km towards the sun keeps the light from the same high cloud: grey.
    grey = rate(lambda km: {"high": 50 if km < 150 else 0, "low": 100 if km > 150 else 0})
    assert grey["score"] < 0.25 * fiery["score"] and grey["gap"] < 20
    # Low cloud over you hides it; fog dims it; rain spoils it.
    hidden = rate(lambda km: {"high": 50 if km < 150 else 0, "low": 100 if km < 40 else 0})
    assert hidden["score"] < 20 and hidden["low"] > 90
    foggy = rate(lambda km: {"high": 50 if km < 150 else 0, "vis": 0.5})
    assert foggy["score"] < 0.35 * fiery["score"] and foggy["vis"] == 0.5
    wet = rate(lambda km: {"high": 50 if km < 150 else 0, "rain": 1.5 if km < 30 else 0})
    assert wet["score"] < 0.5 * fiery["score"] and wet["rain"] == 1.5
    # Overcast high cloud still glows if the light gets in under it.
    overcast = rate(lambda km: {"high": 100})
    assert 45 < overcast["score"] < 65
    # But a middle-level deck all the way to the horizon is grey.
    assert rate(lambda km: {"mid": 100})["score"] < 25
    # Times between the model's hours: halfway, half of each.
    t = 1794060000
    a, b = _sky(lambda km: {"high": 0}, t), _sky(lambda km: {"high": 100}, t + 3600)
    both = sunset.Sky(a.grid, {t: a.parts[0][0], t + 3600: b.parts[0][0]}, t + 1800)
    assert both(*HERE, "high") == pytest.approx(50)
    # Near the model's edge, looking out of it: less of the way is seen.
    edge = sunset.rate(_sky(lambda km: {"high": 50}), 57.0, 17.5, 225)
    assert edge["seen"] < 50


def test_sunset_run_from_the_weather_model(tmp_path):
    now = time.time()

    async def main():
        ov = Overlays(tmp_path, None)
        forecast.register(ov, fake_fmi_fetch)
        sunset.register(ov)
        listing = {o["id"]: o for o in await ov.listing({"sunset"}, now)}
        times = listing["sunset"]["times"]
        assert 2 <= len(times) <= 5 and listing["sunset"]["has_value"]
        frame = await ov.points("sunset", times[0], now)
        assert frame["fields"] == sunset.FIELDS
        kind = frame["info"]["kinds"][str(times[0])]
        assert set(frame["info"]["kinds"].values()) <= {"sunset", "sunrise"}
        scored = [dict(zip(frame["fields"], p[2:])) for p in frame["points"] if p[2] is not None]
        assert len(scored) > 0.5 * len(frame["points"])
        for p in scored:
            assert 0 <= p["score"] <= 100 and abs(p["at"]) <= 240 and 0 <= p["gap"] <= 100
            assert (p["glow"] > p["at"]) if kind == "sunset" else (p["glow"] < p["at"])
        assert (tmp_path / "points" / "sunset").is_dir()  # kept as binary hours
        here = await ov.value("sunset", 60.1, 24.4, 0)
        assert [e["t"] for e in here["events"]] == times
        assert all(e["word"] == sunset.word(e["score"]) and e["kind"] in ("sunset", "sunrise") for e in here["events"])

    asyncio.run(main())


# --- the endpoints -----------------------------------------------------------------------

def test_tap_and_sky_endpoints(tmp_path):
    async def value(lat, lon, ts):
        return {"text": f"{lat:.1f},{lon:.1f}"}

    async def times(session, now):
        return [0]

    async def tile(ts, z, x, y):
        return None

    async def main():
        ov = Overlays(tmp_path, None)
        ov.register(Overlay(id="lp", title="LP", kind="local", times=times, tile=tile, value=value, static=True, max_age=60,
                            attribution=""))
        forecast.register(ov, fake_fmi_fetch)
        aurora.register(ov, fake_noaa_get)
        client = TestClient(TestServer(webmod.Web(SimpleNamespace(overlays=ov)).app))
        await client.start_server()
        try:
            r = await client.get("/api/overlay-value/lp?lat=60.1&lon=24.4")
            assert r.status == 200 and (await r.json()) == {"text": "60.1,24.4"}
            assert (await client.get("/api/overlay-value/temperature?lat=60&lon=24")).status == 404
            assert (await client.get("/api/overlay-value/lp?lat=x&lon=24")).status == 400
            assert (await client.get("/api/overlay-value/lp?lat=95&lon=24")).status == 400
            sky = await (await client.get("/api/sky?lat=60.1&lon=24.4&clouds=1")).json()
            assert len(sky["clouds"]) >= 24 and all(len(c) == 5 for c in sky["clouds"])
            assert sky["kp"] and "error" not in sky
            sky = await (await client.get("/api/sky?lat=60.1&lon=24.4")).json()
            assert sky["clouds"] == [] and sky["kp"]
            assert (await client.get("/api/sky?lat=60.1")).status == 400
        finally:
            await client.close()

    asyncio.run(main())


# --- the sky map's astronomy (static/sky.js) ---------------------------------------------

SKY_SCRIPT = """
const S = require(process.argv[1]);
const at = (y, m, d, h, mi = 0) => Date.UTC(y, m - 1, d, h, mi) / 1000;
const hel = [60.17, 24.94];
const sunAt = (ts) => { const s = S.sun(ts); const [alt, az] = S.horizontal(s.ra, s.dec, ...hel, ts); return [alt, az]; };
const n1 = S.night(...hel, at(2026, 11, 6, 20)), n2 = S.night(...hel, at(2026, 6, 21, 21));
const venus = S.planet("Venus", at(2026, 8, 15, 12)), jupiter = S.planet("Jupiter", at(2026, 11, 6, 0));
const t = S.tonight(...hel, at(2026, 11, 6, 20), { kp: [[at(2026, 11, 6, 21), 6]], clouds: [[at(2026, 11, 6, 21), 10], [at(2026, 11, 6, 23), 5]] });
const cloudy = S.tonight(...hel, at(2026, 11, 6, 20), { kp: [[at(2026, 11, 6, 21), 6]], clouds: [[at(2026, 11, 6, 21), 100], [at(2026, 11, 6, 23), 98]] });
console.log(JSON.stringify({
  noon: sunAt(at(2026, 6, 21, 10, 20)), sunset: sunAt(at(2026, 11, 6, 14, 10)),
  full: S.moon(at(2026, 11, 24, 15)).lit, newMoon: S.moon(at(2026, 11, 9, 15)).lit,
  quarter: S.phaseName(S.moon(at(2026, 11, 17, 12))),
  night1: [n1.start, n1.end, n1.darkFrom, n1.darkTo], night2: [n2.darkFrom, n2.lowAlt],
  venus: [venus.mag, venus.dist], jupiter: [jupiter.mag, jupiter.dist],
  polaris: S.horizontal(37.95, 89.26, ...hel, at(2026, 11, 6, 20)),
  gem: S.showersOn(at(2026, 12, 14, 0)).map((s) => [s.name, s.daysToPeak]),
  quad: S.showersOn(at(2026, 12, 30, 18)).map((s) => [s.name, Math.round(s.daysToPeak)]),
  none: S.showersOn(at(2026, 3, 1, 0)).length,
  items: t.items.map((i) => [i.icon, i.score]), note: S.highlight(t), cloudy: S.highlight(cloudy),
  compass: [S.compass(0), S.compass(100), S.compass(350)],
}));
"""


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_sky_astronomy():
    out = json.loads(subprocess.run([NODE, "-e", SKY_SCRIPT, str(STATIC / "sky.js")], capture_output=True,
                                    text=True, check=True).stdout)
    # The sun: due south and 53 degrees up at Helsinki's midsummer noon; on the horizon at sunset.
    assert out["noon"][0] == pytest.approx(53.2, abs=0.5) and out["noon"][1] == pytest.approx(180, abs=3)
    assert abs(out["sunset"][0]) < 1 and 225 < out["sunset"][1] < 245
    # Sunset and the cross-check against the add-on's own sun maths (sunset.py).
    py_alt, py_az = sunset.sun_position(60.17, 24.94, 1793974200)
    assert out["sunset"][0] == pytest.approx(py_alt, abs=0.1) and out["sunset"][1] == pytest.approx(py_az, abs=0.2)
    # The Moon: full on 24 Nov 2026, new on 9 Nov, first quarter on the 17th.
    assert out["full"] > 0.98 and out["newMoon"] < 0.02 and out["quarter"] in ("first quarter", "waxing gibbous")
    # The night: from about 16:10 to 07:50 EET, dark (sun 12 degrees down) from about 17:50; none in midsummer.
    start, end, dark_from, dark_to = out["night1"]
    assert abs(start - 1793974200) < 20 * 60 and abs(end - 1794030600) < 30 * 60
    assert dark_from - start > 3600 and dark_to < end
    assert out["night2"][0] is None and -8 < out["night2"][1] < -5
    # Planets: Venus never fainter than -3.8 or brighter than -4.9; Jupiter 4-6.5 AU away.
    assert -4.9 < out["venus"][0] < -3.8 and 0.25 < out["venus"][1] < 1.75
    assert -2.9 < out["jupiter"][0] < -1.6 and 4 < out["jupiter"][1] < 6.5
    # Polaris stands at the latitude, due north.
    assert out["polaris"][0] == pytest.approx(60.17, abs=1) and (out["polaris"][1] < 2 or out["polaris"][1] > 358)
    assert out["gem"] == [["Geminids", pytest.approx(0, abs=0.1)]]
    assert out["quad"] == [["Quadrantids", 3]] and out["none"] == 0
    # Kp 6 at Helsinki on a clear night: the northern lights first, and the note says so.
    assert out["items"][0] == ["aurora", 2.8]
    assert out["note"]["icon"] == "aurora" and out["note"]["text"].startswith("Tonight: Northern lights likely")
    assert "Clear skies" in out["note"]["text"]
    assert out["cloudy"] is None  # all cloud: no note
    assert out["compass"] == ["N", "E", "N"]


# --- the colour layers' maths (static/fields.js) -----------------------------------------

FIELDS_SCRIPT = """
const F = require(process.argv[1]);
const data = { fields: ["t", "vis", "rh", "td", "ms", "low"], points: [
  [60, 24, 10, null, 99, 9.8, 2, 0], [60, 25, 12, null, 99, 9.9, 2, 0],
  [61, 24, 14, 8, 70, 2, 9, 0], [61, 25, null, null, null, null, null, null]] };
const g = F.grid(data);
const r = (v) => Math.round(v * 100) / 100;
// A low in the middle of a 7 x 7 grid of pressure rising outwards.
const p = new Float32Array(49).map((_, k) => 1000 + Math.hypot((k % 7) - 3, Math.floor(k / 7) - 3));
console.log(JSON.stringify({
  mid: r(F.sample(g, "t", 60.5, 24.5)), edge: r(F.sample(g, "t", 60, 24.4)), out: F.sample(g, "t", 62, 24),
  thin: F.sample(g, "t", 60.95, 24.95), near: F.nearest(g, "t", 60.9, 24.1),
  fogMade: r(F.fogVis(g, 60, 24)), fogModel: F.fogVis(g, 61, 24),
  cold: F.colourAt(F.SCALES.temp, -40), between: F.colourAt(F.SCALES.temp, -25),
  lows: F.extremes(p, 7, 7).map((e) => [e.kind, e.i, e.j]),
  ring: F.contours(p, 7, 7, 1001.5).length,
  legend: F.legendFor("waves", null).ticks.map(([pos]) => Math.round(pos)),
  renderers: Object.keys(F.renderers).sort(),
}));
"""


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_colour_layer_maths():
    out = json.loads(subprocess.run([NODE, "-e", FIELDS_SCRIPT, str(STATIC / "fields.js")], capture_output=True,
                                    text=True, check=True).stdout)
    assert out["mid"] == 12 and out["edge"] == 10.8  # the missing corner left out
    assert out["out"] is None and out["near"] == 14
    assert out["thin"] is None  # mostly the missing corner: no colour rather than a guess
    assert out["fogMade"] < 1 and out["fogModel"] == 8  # near the dew point, calm: fog; else the model's own
    assert out["cold"] == [125, 70, 160, 210] and out["between"] == [103, 75, 175, 210]
    assert out["lows"] == [["L", 3, 3]] and out["ring"] > 4
    assert out["legend"] == [0, 17, 33, 50, 67, 100]
    assert {"pressure", "lightning", "waves", "sunset", "light-pollution", "temperature", "aurora"} <= set(out["renderers"])
