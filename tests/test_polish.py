"""0.75.0: the stall watcher, cached counts and the ship's latest passage."""

import asyncio
import time
from pathlib import Path

from ferrywatch import stalls
from ferrywatch.store import Store

MMSI = 230184000


def test_latest_passage_is_the_newest_of_either_route(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.latest_passage(MMSI) is None
    base = {"zone_id": 1, "lat": 60.0, "lon": 24.4, "sog": 17.0}
    store.add_passage({**base, "mmsi": MMSI, "ts": 1000, "route": "outer", "direction": "east"})
    pid = store.add_passage({**base, "mmsi": MMSI, "ts": 5000, "route": "inner", "direction": "west"})
    store.set_porkkala_ts(pid, 5100)
    last = store.latest_passage(MMSI)
    assert last == {"ts": 5000, "route": "inner", "direction": "west", "porkkala_ts": 5100}


def test_position_count_follows_inserts_and_prunes(tmp_path):
    from ferrywatch.tracker import Position

    store = Store(tmp_path / "t.db")
    assert store.position_count() == 0
    for i in range(5):
        store.insert_position(Position(MMSI, 1000 + i, 60.0, 24.0, 10.0, 90.0, 90))
    assert store.position_count() == 5  # counted from the cache, kept up to date
    store.prune(1003)
    assert store.position_count() == 2
    store._count_kept = None
    assert store.position_count() == 2  # and the same when counted afresh


def test_stall_watch_names_the_place_that_holds_the_loop():
    watch = stalls.StallWatch()
    watch.roots = (Path(__file__).parent,)  # count this file as the app's own code

    def hold():
        time.sleep(0.6)  # the loop is stuck here

    async def main():
        watch.start(asyncio.get_running_loop())
        await asyncio.sleep(0.3)
        hold()
        await asyncio.sleep(0.5)
        watch.stop()

    asyncio.run(main())
    summary = watch.summary()
    assert summary["stalls"] >= 1
    assert any("test_polish.py" in p["where"] and "hold" in p["where"] for p in summary["places"])
    assert summary["places"][0]["worst"] >= 0.3


def test_stall_watch_is_quiet_when_the_loop_is_free():
    watch = stalls.StallWatch()

    async def main():
        watch.start(asyncio.get_running_loop())
        for _ in range(8):
            await asyncio.sleep(0.1)
        watch.stop()

    asyncio.run(main())
    assert watch.summary()["stalls"] == 0


def test_json_answers_go_out_gzipped_when_big():
    import gzip
    import json

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from ferrywatch.web import compress_answers

    async def big(request):
        return web.json_response({"rows": [[60.1, 24.4, 1795700000 + i] for i in range(400)]})

    async def small(request):
        return web.json_response({"ok": True})

    async def main():
        app = web.Application(middlewares=[compress_answers])
        app.router.add_get("/big", big)
        app.router.add_get("/small", small)
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/big", headers={"Accept-Encoding": "gzip"}, auto_decompress=False)
            raw = await r.read()
            assert r.headers.get("Content-Encoding") == "gzip"
            assert len(json.loads(gzip.decompress(raw))["rows"]) == 400
            r = await client.get("/big", headers={"Accept-Encoding": "identity"}, auto_decompress=False)
            assert "Content-Encoding" not in r.headers
            r = await client.get("/small", headers={"Accept-Encoding": "gzip"}, auto_decompress=False)
            assert "Content-Encoding" not in r.headers

    asyncio.run(main())


def test_passages_since_is_compact_and_oldest_first(tmp_path):
    store = Store(tmp_path / "t.db")
    base = {"zone_id": 1, "lat": 60.0, "lon": 24.4, "sog": 17.0, "mmsi": MMSI}
    store.add_passage({**base, "ts": 1000, "route": "inner", "direction": "east"})
    pid = store.add_passage({**base, "ts": 5000, "route": "outer", "direction": "west"})
    store.set_porkkala_ts(pid, 5050)
    store.add_passage({**base, "ts": 6000, "route": "phone", "direction": "west"})  # not a Porkkala route
    assert store.passages_since(900) == [[1000, MMSI, "inner", "east"], [5050, MMSI, "outer", "west"]]
    assert store.passages_since(2000) == [[5050, MMSI, "outer", "west"]]
