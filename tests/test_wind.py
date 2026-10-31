"""The wind forecast overlay: the FMI point query, matching answers to grid points, the run,
the MEPS fallback, the check against the stations, and the panel's grid maths under Node."""

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

from ferrywatch import simsea, wind
from ferrywatch.overlays import Overlays
from ferrywatch.stations import parse_coverage, parse_multipoint

T0 = 1792929600  # 2026-10-25 12:00:00 UTC


def test_grid_and_request():
    assert len(wind.GRID) == 126 and wind.GRID[0] == (59.0, 21.0) and wind.GRID[-1] == (60.8, 30.35)
    assert wind.BOUNDS == (59.0, 21.0, 60.8, 30.35)
    params = wind.request_params(wind.QUERIES[0], wind.GRID[:3], T0 + 1500)
    q = dict(params)
    assert q["storedquery_id"] == "fmi::forecast::harmonie::surface::point::multipointcoverage"
    assert q["parameters"] == "WindSpeedMS,WindGust,WindDirection" and q["timestep"] == "60"
    assert q["starttime"] == "2026-10-25T12:00:00Z" and q["endtime"] == "2026-10-27T12:00:00Z"
    assert [v for k, v in params if k == "latlon"] == ["59.00,21.00", "59.00,21.55", "59.00,22.10"]


def test_forecast_answer_parses_with_the_station_parser():
    text = simsea.fake_forecast(wind.request_params(wind.QUERIES[0], wind.GRID[:2], T0))
    fields, rows = parse_coverage(text, wind.PARAMS)
    assert fields == ["windspeedms", "windgust", "winddirection"]
    assert len(rows) == 2 * 49 and rows[0][:3] == (59.0, 21.0, T0) and len(rows[0][3]) == 3
    # The station parser, now sharing the coverage code, still reads the stations' answers.
    sites, series, sfields = parse_multipoint(simsea.fake_multipoint(
        {"storedquery_id": "fmi::observations::weather::multipointcoverage", "parameters": "windspeedms,winddirection",
         "starttime": "2026-10-25T11:00:00Z", "timestep": 10}, now=T0))
    assert sfields == ["windspeedms", "winddirection"] and len(series[100997]) == 5


def test_rows_matched_to_asked_points():
    wanted = [(59.0, 21.0), (59.0, 21.55), (59.3, 21.0)]
    rows = [(59.3, 21.0, T0, [5.0]), (59.00001, 21.55, T0, [6.0]), (59.0, 21.0, T0, [7.0])]
    assert {i: s[0][1][0] for i, s in wind.assign(rows, wanted).items()} == {2: 5.0, 1: 6.0, 0: 7.0}
    # Coordinates of somewhere else (a model cell, a place), one per asked point: in order.
    moved = [(59.2, 21.3, T0, [5.0]), (59.4, 21.9, T0, [6.0]), (59.7, 21.1, T0, [7.0])]
    assert {i: s[0][1][0] for i, s in wind.assign(moved, wanted).items()} == {0: 5.0, 1: 6.0, 2: 7.0}
    # Fewer points than asked and far off: only what matches.
    assert list(wind.assign([(59.0, 21.0, T0, [1.0]), (61.0, 25.0, T0, [2.0])], wanted)) == [0]


def test_run_from_answers_drops_thin_hours():
    batches = [[(59.0, 21.0), (59.0, 21.55)]]
    rows = [(59.0, 21.0, T0, [5.0, 8.0, 200.0]), (59.0, 21.55, T0, [6.0, None, 210.0]),
            (59.0, 21.0, T0 + 3600, [None, None, None]), (59.0, 21.55, T0 + 3600, [None, 4.0, 220.0])]
    # Fields in another order than asked are put in the run's order.
    fields = ["windspeedms", "windgust", "winddirection"]
    run = wind.build_run([(fields, rows)], batches)
    assert run["times"] == [T0] and run["fields"] == ["ms", "gust", "dir"]
    assert run["values"][str(T0)] == [[5.0, 8.0, 200.0], [6.0, None, 210.0]]
    swapped = [(la, lo, t, [v[2], v[0], v[1]]) for la, lo, t, v in rows]
    run2 = wind.build_run([(["winddirection", "windspeedms", "windgust"], swapped)], batches)
    assert run2["values"] == run["values"]
    with pytest.raises(ValueError):
        wind.build_run([(fields, rows[2:])], batches)


def test_whole_grid_in_batches_and_meps_fallback():
    asked = []

    async def fetch(params):
        q = dict(params)
        asked.append((q["storedquery_id"], sum(k == "latlon" for k, _ in params)))
        if "harmonie" in q["storedquery_id"]:
            raise aiohttp.ClientResponseError(None, (), status=400, message="Bad Request")
        return simsea.fake_forecast(params)

    queries = list(wind.QUERIES)
    run = asyncio.run(wind.fetch_run(None, T0, fetch, queries))
    assert len(run["points"]) == 126 and len(run["times"]) == 49 and run["times"][0] == T0
    assert all(len(rows) == 126 and all(r[0] is not None for r in rows) for rows in run["values"].values())
    assert asked == [(wind.QUERIES[0], 42), (wind.QUERIES[1], 42), (wind.QUERIES[1], 42), (wind.QUERIES[1], 42)]
    assert queries[0] == wind.QUERIES[1]  # MEPS first from now on
    asked.clear()
    asyncio.run(wind.fetch_run(None, T0, fetch, queries))
    assert [q for q, _ in asked] == [wind.QUERIES[1]] * 3

    async def down(params):
        raise aiohttp.ClientResponseError(None, (), status=503, message="Service Unavailable")

    with pytest.raises(aiohttp.ClientResponseError):  # not a reason to try the other name
        asyncio.run(wind.fetch_run(None, T0, down, list(wind.QUERIES)))


def test_speed_between_grid_points_and_check_against_stations():
    run = {"times": [T0, T0 + 3600], "fields": ["ms", "gust", "dir"],
           "points": [[59.0, 21.0], [59.0, 22.0], [60.0, 21.0], [60.0, 22.0]],
           "values": {str(T0): [[4.0, 6, 0], [8.0, 9, 0], [4.0, 5, 0], [None, None, None]],
                      str(T0 + 3600): [[10.0, 12, 0]] * 4}}
    assert wind.speed_at(run, T0, 59.0, 21.5) == pytest.approx(6.0)
    assert wind.speed_at(run, T0, 59.5, 21.5) == pytest.approx((4 + 8 + 4) / 3)  # a missing corner is left out
    assert wind.speed_at(run, T0, 61.0, 21.5) is None
    now = T0 + 600
    stations = [{"ws": 7.0, "lat": 59.0, "lon": 21.5, "wind_ts": now - 300},
                {"ws": 3.0, "lat": 59.0, "lon": 21.0, "wind_ts": now - 300},
                {"ws": 5.0, "lat": 60.0, "lon": 21.0, "wind_ts": now - 300},
                {"ws": 2.0, "lat": 63.0, "lon": 21.0, "wind_ts": now - 300},  # outside the grid
                {"ws": 9.0, "lat": 59.0, "lon": 22.0, "wind_ts": now - 7200}]  # too old
    assert wind.compare(run, stations, now) == {"n": 3, "mean": 1.0, "bias": -0.3}
    assert wind.compare(run, stations[:2], now) is None
    assert wind.check_text({"n": 12, "mean": 1.6, "bias": 0.8}) == (
        "Wind forecast against 12 FMI stations' latest readings: 1.6 m/s apart on average, "
        "the forecast 0.8 m/s stronger overall.")
    assert wind.check_text({"n": 12, "mean": 1.2, "bias": -0.2}).endswith("1.2 m/s apart on average.")


def test_listing_carries_the_check(tmp_path):
    now = time.time()
    stations = SimpleNamespace(fetched=now, stations={
        s[0]: {"ws": simsea._value("windspeedms", s[0], s[4], s[2], now - 300), "lat": s[2], "lon": s[3],
               "wind_ts": now - 300} for s in simsea.SIM_STATIONS})
    overlays = Overlays(tmp_path, None)
    wind.register(overlays, stations, simsea.fake_forecast_fetch)

    async def run():
        off = await overlays.listing(set(), now)
        on = await overlays.listing({"wind"}, now)
        return off[0], on[0]

    off, on = asyncio.run(run())
    assert off["times"] is None and off["check"] is None
    assert len(on["times"]) == 49 and on["check"].startswith("Wind forecast against 11 FMI stations")
    stations.fetched = now - 2 * 3600  # old readings aren't compared
    assert asyncio.run(overlays.listing({"wind"}, now))[0]["check"] is None


# --- the panel's grid maths (static/wind.js, under Node) -----------------------------------

WIND_JS = Path(__file__).resolve().parents[1] / "ferry_watch" / "app" / "ferrywatch" / "static" / "wind.js"
NODE = shutil.which("node")

SCRIPT = """
const W = require(process.argv[1]);
const data = { fields: ["ms", "gust", "dir"], points: [
  [60, 21, 10, 14, 270], [60, 22, 10, 14, 270], [59, 21, 10, 14, 180], [59, 22, null, null, null]] };
const g = W.grid(data);
const r = (a) => a && a.map((v) => Math.round(v * 100) / 100);
console.log(JSON.stringify({
  lats: g.lats, lons: g.lons, ms: Array.from(g.ms).map((v) => (Number.isNaN(v) ? null : v)),
  uvWest: r(W.uv(10, 270)), uvSouth: r(W.uv(10, 180)),
  top: r(W.sample(g, 60, 21.5)), corner: r(W.sample(g, 59, 21)), mid: r(W.sample(g, 59.5, 21.5)),
  outside: W.sample(g, 61, 21.5), cell: W.cell([0, 1, 2], 1.5),
  thin: [W.thin(50), W.thin(20), W.thin(12)],
  colours: [W.color(3.4), W.color(3.6), W.color(10.4), W.color(11), W.color(25)], step: W.stepOf(14),
}));
"""


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_panel_grid_maths():
    out = json.loads(subprocess.run([NODE, "-e", SCRIPT, str(WIND_JS)], capture_output=True, text=True,
                                    check=True).stdout)
    assert out["lats"] == [59, 60] and out["lons"] == [21, 22]
    assert out["ms"] == [10, None, 10, 10]  # row by row from the south
    assert out["uvWest"] == [10, 0] and out["uvSouth"] == [0, 10]  # from the west blows east, from the south north
    assert out["top"] == [10, 0, 10] and out["corner"] == [0, 10, 10]
    # Between a westerly and a southerly (the missing corner left out): north-east, speed kept at 10.
    assert out["mid"] == [6.67, 3.33, 10]
    assert out["outside"] is None and out["cell"] == [1, 0.5]
    assert out["thin"] == [1, 2, 3]
    assert out["colours"] == ["#0ea5e9", "#16a34a", "#ca8a04", "#ea580c", "#7c3aed"] and out["step"] == 4
