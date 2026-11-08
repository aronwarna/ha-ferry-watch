"""Nature reserves from OpenStreetMap (reserves.py) and the panel's maths (static/reserves.js)."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ferrywatch import reserves as R
from ferrywatch.simsea import fake_reserves, fake_reserves_fetch

STATIC = Path(__file__).resolve().parents[1] / "ferry_watch" / "app" / "ferrywatch" / "static" / "reserves.js"
NODE = shutil.which("node")


def answer():
    return {"elements": fake_reserves()}


def test_parse_kinds_links_and_rings():
    got = {r["id"]: r for r in R.parse_overpass(answer())}
    assert set(got) == {"w990001", "r990002", "r990003"}
    islets = got["w990001"]
    assert islets["kind"] == "Private nature reserve"
    assert islets["other_names"] == ["Rönnbåda skär"]
    assert islets["access"] == ["No access Apr 01–Aug 15"]
    assert islets["ref"] == "YSA201234" and islets["since"] == "1994-05-12"
    park = got["r990002"]
    assert park["kind"] == "National park" and park["operator"] == "Metsähallitus"
    assert park["wikipedia"] == "https://fi.wikipedia.org/wiki/Tammisaaren_saariston_kansallispuisto"
    assert park["wikidata"] == "Q1468155"
    assert len(park["rings"]) == 2  # the outline and its hole
    # The hole is taken off: about 6.8 x 5.8 km less 1.2 x 1.2 km, give or take the corner.
    assert 25e6 < park["area_m2"] < 40e6
    # Two outer ways joined into one ring.
    assert len(got["r990003"]["rings"]) == 1
    assert got["r990003"]["rings"][0][0] == got["r990003"]["rings"][0][-1]


def test_bad_links_dropped_and_access_rules():
    data = {"elements": [{"type": "way", "id": 1, "geometry": [
        {"lat": 60, "lon": 24}, {"lat": 60, "lon": 24.01}, {"lat": 60.01, "lon": 24.01}, {"lat": 60, "lon": 24}],
        "tags": {"leisure": "nature_reserve", "website": "javascript:alert(1)", "wikidata": "nope",
                 "access": "no", "boat:conditional": "no @ (May 01-Jul 31)", "landing": "no"}}]}
    (r,) = R.parse_overpass(data)
    assert r["website"] is None and r["wikidata"] is None and r["name"] is None
    assert r["access"] == ["No access", "Boats: no access May 01–Jul 31", "Landing: no"]
    assert r["kind"] == "Nature reserve"


def test_simplify_keeps_corners():
    line = [(24 + i * 1e-5, 60.0) for i in range(100)] + [(24.001, 60.001), (24.0, 60.0)]
    out = R._simplify(line, 6)
    assert len(out) <= 5 and out[0] == out[-1]


def test_in_view_fetches_saves_and_reads_back(tmp_path):
    calls = []

    async def fetch(query):
        calls.append(query)
        return await fake_reserves_fetch(query)

    async def run():
        res = R.Reserves(tmp_path, None, fetch)
        first = res.in_view(59.96, 23.96, 60.0, 24.2)
        assert first["pending"] and not first["reserves"]
        await res._task
        second = res.in_view(59.96, 23.96, 60.0, 24.2)
        assert not second["pending"]
        assert {r["id"] for r in second["reserves"]} >= {"w990001", "r990002"}
        n = len(calls)
        # From disk in a new instance, without asking again.
        again = R.Reserves(tmp_path, None, fetch).in_view(59.96, 23.96, 60.0, 24.2)
        assert again["reserves"] and len(calls) == n
        assert "out tags geom" in calls[0] and '"leisure"="nature_reserve"' in calls[0]
        assert R.Reserves(tmp_path, None, fetch).in_view(55, 15, 65, 30)["error"] == "zoom in"

    asyncio.run(run())


def test_failure_is_reported_and_not_retried_at_once(tmp_path):
    calls = []

    async def fetch(query):
        calls.append(query)
        raise ValueError("HTTP 504: busy")

    async def run():
        res = R.Reserves(tmp_path, None, fetch)
        res.in_view(59.96, 24.0, 59.98, 24.1)
        await res._task
        out = res.in_view(59.96, 24.0, 59.98, 24.1)
        assert out["error"] == "HTTP 504: busy" and not out["pending"]
        assert len(calls) == 1

    asyncio.run(run())


SCRIPT = """
const R = require(process.argv[1]);
const sq = (w, s, e, n) => [[s, w], [s, e], [n, e], [n, w], [s, w]];
const park = { id: "r1", area_m2: 4e6, bbox: [24, 60, 24.04, 60.02], rings: [sq(24, 60, 24.04, 60.02), sq(24.01, 60.005, 24.02, 60.01)] };
const small = { id: "w2", area_m2: 1e4, bbox: [24.03, 60.012, 24.035, 60.015], rings: [sq(24.03, 60.012, 24.035, 60.015)] };
console.log(JSON.stringify({
  inPark: R.contains(park, 60.002, 24.002),
  inHole: R.contains(park, 60.007, 24.015),
  outside: R.contains(park, 60.03, 24.002),
  both: R.at([park, small], 60.013, 24.032).map((r) => r.id),
  label: R.contains(park, ...R.labelPoint(park)),
  areas: [R.areaText(500), R.areaText(52000), R.areaText(3.4e6), R.areaText(123e6)],
  since: [R.sinceText("1994-05-12"), R.sinceText("1989"), R.sinceText("before 1950")],
  links: R.links({ id: "r9", name: "Tammisaari", kind: "National park", wikidata: "Q1" }).map((l) => l[0]),
  privateLinks: R.links({ id: "w9", name: "Luoto", kind: "Private nature reserve" }).map((l) => l[0]),
}));
"""


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_panel_geometry():
    out = json.loads(subprocess.run([NODE, "-e", SCRIPT, str(STATIC)], capture_output=True, text=True,
                                    check=True).stdout)
    assert out["inPark"] and not out["inHole"] and not out["outside"]
    assert out["both"] == ["w2", "r1"]  # smallest first
    assert out["label"]  # the middle is the hole, so the label moves into the park
    assert out["areas"] == ["500 m²", "5.2 ha", "3.4 km²", "123 km²"]
    assert out["since"] == ["1994", "1989", "before 1950"]
    assert out["links"] == ["Find on luontoon.fi", "Wikidata", "OpenStreetMap"]
    assert out["privateLinks"] == ["OpenStreetMap"]


def test_broken_outlines_are_closed():
    # A relation whose outer ring is three ways, one of them ending short of the next (a gap),
    # and a reserve drawn as an open way: both still get a ring to fill.
    pt = lambda lat, lon: {"lat": lat, "lon": lon}  # noqa: E731
    data = {"elements": [
        {"type": "relation", "id": 7, "tags": {"leisure": "nature_reserve", "name": "Gap"}, "members": [
            {"type": "way", "role": "outer", "geometry": [pt(60.0, 24.0), pt(60.0, 24.02)]},
            {"type": "way", "role": "outer", "geometry": [pt(60.01, 24.02), pt(60.0, 24.0201)]},  # reversed, 10 m short
            {"type": "way", "role": "outer", "geometry": [pt(60.01, 24.02), pt(60.01, 24.0), pt(60.0, 24.0)]}]},
        {"type": "way", "id": 8, "tags": {"leisure": "nature_reserve"},
         "geometry": [pt(60.1, 24.0), pt(60.1, 24.02), pt(60.11, 24.02), pt(60.11, 24.0)]},
    ]}
    got = {r["id"]: r for r in R.parse_overpass(data)}
    assert set(got) == {"r7", "w8"}
    for r in got.values():
        assert len(r["rings"]) == 1 and r["rings"][0][0] == r["rings"][0][-1]
        assert 1.0e6 < r["area_m2"] < 1.4e6  # about 1.1 x 1.1 km
