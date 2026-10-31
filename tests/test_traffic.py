import asyncio
import time

import aiohttp

from ferrywatch.simulate import fake_traffic
from ferrywatch.store import Store
from ferrywatch.traffic import Traffic, parse_locations


def feat(mmsi, lat, lon, ts, sog=12.0, cog=90.0, heading=91):
    return {"mmsi": mmsi, "type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"mmsi": mmsi, "sog": sog, "cog": cog, "heading": heading,
                           "timestampExternal": int(ts * 1000)}}


def test_parse_drops_stale_tracked_and_unavailable():
    now = time.time()
    data = {"features": [
        feat(1, 60.0, 24.5, now - 30),
        feat(2, 60.0, 24.6, now - 3600),  # silent for an hour
        feat(3, 60.0, 24.7, now - 10),  # already on the map
        feat(4, 60.0, 24.8, now - 10, sog=102.3, cog=360, heading=511),
        {"type": "Feature", "geometry": {"coordinates": [24, 60]}, "properties": {}},  # no MMSI
    ]}
    rows = parse_locations(data, now, {3})
    assert [r[0] for r in rows] == [1, 4]
    assert rows[0][3:] == (12.0, 90.0, 91)
    assert rows[1][3:] == (None, None, None)


def test_rows_join_the_directory_and_are_cached(tmp_path):
    store = Store(tmp_path / "t.db")
    store.replace_vessels([(1, "ARUNA", "ARUNA", 70, 120, "HAMINA", 1)])
    calls = []

    async def fetch(lat, lon, radius, from_ms):
        calls.append((lat, lon, radius))
        return {"features": [feat(1, 60.0, 24.5, time.time()), feat(5, 60.1, 24.4, time.time())]}

    async def run():
        t = Traffic(None, store, lambda: set(), fetch)
        rows = await t.around(60.03, 24.47, 33)
        assert rows[0][6:] == ["Aruna", 70, 120, "HAMINA"]
        assert rows[1][6:] == [None, None, None, None]
        # A slightly moved view shares the answer; the radius is rounded up and capped.
        await t.around(60.04, 24.45, 31)
        assert calls == [(60.0, 24.4, 40)]
        await t.around(60.0, 24.4, 500)
        assert calls[-1][2] == 60

    asyncio.run(run())


def test_error_returns_last_answer(tmp_path):
    store = Store(tmp_path / "t.db")
    n = []

    async def fetch(lat, lon, radius, from_ms):
        n.append(1)
        if len(n) > 1:
            raise aiohttp.ClientError("blocked")
        return {"features": [feat(1, 60.0, 24.5, time.time())]}

    async def run():
        t = Traffic(None, store, set, fetch)
        assert len(await t.around(60, 24.4, 20)) == 1
        t._cache = {k: (0, v[1]) for k, v in t._cache.items()}  # expire it
        assert len(await t.around(60, 24.4, 20)) == 1 and t.error == "blocked"
        assert await t.around(59.5, 23.0, 20) == []

    asyncio.run(run())


def test_simulated_traffic_parses():
    data = fake_traffic(60.0, 24.6, 60)
    assert parse_locations(data, time.time(), set())
