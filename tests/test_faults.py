import asyncio
import time

import aiohttp

from ferrywatch import faults as faults_mod
from ferrywatch.faults import Faults, parse_faults
from ferrywatch.simulate import fake_faults


def feature(lon, lat, **props):
    base = {"id": 7, "entry_timestamp": "2026-10-03T06:12:00Z", "fixed_timestamp": None, "type": "Unlit",
            "fixed": False, "aton_name_fi": "Rönnskär", "aton_name_sv": "Rönnskär", "aton_type": "Lighthouse",
            "fairway_name_fi": "Porkkalan väylä"}
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": {**base, **props}}


def test_parse_keeps_open_faults_in_the_area():
    data = {"features": [
        feature(24.36, 59.92),
        feature(24.4, 59.9, id=8, fixed=True),  # fixed already
        feature(21.0, 60.4, id=9),  # Archipelago Sea, outside
        feature(24.5, 59.95, id=10, aton_name_fi="Kallbådan", aton_name_sv="Kallbådan grund"),
        {"type": "Feature", "geometry": None, "properties": {}},
    ]}
    rows = parse_faults(data)
    assert [r["id"] for r in rows] == [7, 10]
    first = rows[0]
    assert first["name"] == "Rönnskär" and first["fairway"] == "Porkkalan väylä" and first["type"] == "Unlit"
    assert first["since"] == 1791007920.0
    assert rows[1]["name"] == "Kallbådan / Kallbådan grund"
    assert parse_faults(None) == [] and parse_faults({}) == []


def test_simulated_faults_parse():
    rows = parse_faults(fake_faults())
    assert len(rows) == 2 and all(r["since"] < time.time() for r in rows)


def test_cached_and_kept_on_error():
    calls = []

    async def fetch():
        calls.append(1)
        if len(calls) > 1:
            raise aiohttp.ClientError("down")
        return {"features": [feature(24.36, 59.92)]}

    async def run():
        f = Faults(None, fetch)
        assert len(await f.get()) == 1
        assert len(await f.get()) == 1 and len(calls) == 1  # from the cache
        f.fetched = 0
        assert len(await f.get()) == 1 and f.error == "down"  # the old list stays
        assert len(calls) == 2
        # and the next try comes after a few minutes, not the full half hour
        assert faults_mod.CACHE_SECONDS - (time.time() - f.fetched) <= faults_mod.RETRY + 1

    asyncio.run(run())
