import asyncio
import gzip
import json
import time

import aiohttp

from ferrywatch import harbours as harbours_mod
from ferrywatch.harbours import Harbours, overpass_query, parse_overpass
from ferrywatch.simsea import fake_overpass

# The shape of an Overpass API JSON answer to `out center tags`: nodes carry lat/lon,
# ways and relations a "center".
FIXTURE = {
    "version": 0.6, "generator": "Overpass API 0.7.62.1 084b4234",
    "osm3s": {"timestamp_osm_base": "2026-10-19T08:00:00Z", "copyright": "The data included in this document is "
              "from www.openstreetmap.org. The data is made available under ODbL."},
    "elements": [
        {"type": "node", "id": 101, "lat": 60.1468, "lon": 24.9866,
         "tags": {"leisure": "marina", "name": "HSK", "seamark:type": "harbour",
                  "seamark:small_craft_facility:category": "fuel_station;showers;water_tap;pump-out",
                  "website": "www.hsk.fi", "contact:phone": "+358 9 123;+358 40 1", "depth": "3,2 m"}},
        {"type": "way", "id": 202, "center": {"lat": 60.1470, "lon": 24.9869},
         "tags": {"leisure": "marina", "name": "hsk", "sauna": "yes", "toilets": "no", "electricity": "yes"}},
        {"type": "relation", "id": 303, "center": {"lat": 59.9833, "lon": 24.425},
         "tags": {"harbour": "yes", "name": "Porkkalan Marina Dragesviken", "restaurant": "yes",
                  "website": "javascript:alert(1)"}},
        {"type": "node", "id": 404, "lat": 60.0, "lon": 24.5, "tags": {"harbour": "yes"}},  # nameless, not a marina
        {"type": "node", "id": 505, "lat": 59.97, "lon": 24.40, "tags": {"seamark:type": "harbour",
                                                                          "seamark:harbour:category": "yacht"}},
        {"type": "way", "id": 606, "center": {"lat": 61.5, "lon": 23.8}, "tags": {"leisure": "marina",
                                                                                   "name": "Tampere"}},
        {"type": "way", "id": 707, "tags": {"leisure": "marina", "name": "No centre"}},
    ],
}


def test_parse_overpass_fixture():
    rows = {r[0]: r for r in parse_overpass(FIXTURE)}
    assert set(rows) == {"n101", "r303", "n505"}  # the way 202 merged into 101; inland and nameless dropped
    hsk = rows["n101"]
    assert hsk[3] == "HSK" and hsk[4] == "Marina"
    assert hsk[5] == ["fuel", "shower", "sauna", "electricity", "water", "waste"]  # toilets=no stays out
    assert hsk[6] == 3.2 and hsk[8] == "https://www.hsk.fi" and hsk[9] == "+358 9 123"
    assert rows["r303"][5] == ["restaurant"] and rows["r303"][8] is None  # no javascript: links
    assert rows["n505"][3] == "" and rows["n505"][4] == "Yacht harbour"
    assert parse_overpass(None) == [] and parse_overpass({}) == []


def test_query_asks_for_the_three_kinds_along_the_coast():
    q = overpass_query()
    assert q.startswith("[out:json]") and q.endswith("out center tags;")
    for f in ('"leisure"="marina"', '"harbour"="yes"', '"seamark:type"="harbour"'):
        assert f"nwr[{f}](poly:" in q


def test_download_on_demand_stored_and_refreshed_monthly(tmp_path, monkeypatch, caplog):
    calls = []

    async def fetch(query):
        calls.append(query)
        if fail:
            raise aiohttp.ClientError("blocked")
        return fake_overpass()

    path = tmp_path / "harbours.json.gz"

    async def run():
        nonlocal fail
        h = Harbours(path, None, fetch)
        first = h.in_view(59.9, 24.3, 60.1, 24.6)
        assert first["pending"] and first["harbours"] == []  # nothing until the download is done
        await h._task
        got = h.in_view(59.9, 24.3, 60.1, 24.6)
        assert not got["pending"] and {r[3] for r in got["harbours"]} == {
            "Porkkalan Marina Dragesviken", "Rönnskär", "Kantvikin venesatama", "Upinniemen satama"}
        assert h.in_view(59.97, 24.41, 59.99, 24.44)["harbours"][0][3] == "Porkkalan Marina Dragesviken"
        with gzip.open(path, "rt") as f:
            assert len(json.load(f)["rows"]) == 4
        assert len(calls) == 1
        # Read back from disk by a new instance, with no download.
        h2 = Harbours(path, None, fetch)
        assert len(h2.in_view(59, 19, 66, 30)["harbours"]) == 4 and h2._task is None
        # A month later it is fetched again; a failure keeps the old rows and logs once.
        fail = True
        h2.updated = time.time() - 31 * 86400
        h2.in_view(59, 19, 66, 30)
        await h2._task
        assert h2.error == "blocked" and len(h2.in_view(59, 19, 66, 30)["harbours"]) == 4
        assert h2._task.done() and len(calls) == 2  # no retry within the hour
        # Rows are let go when nobody has asked for a while.
        monkeypatch.setattr(harbours_mod, "IDLE", 0)
        h2._used = 0
        h2._release()
        assert h2.rows is None

    fail = False
    with caplog.at_level("WARNING"):
        asyncio.run(run())
    assert sum("Overpass" in r.message for r in caplog.records) == 1
