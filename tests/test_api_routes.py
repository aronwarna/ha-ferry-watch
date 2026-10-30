"""The routes, auto-route, weather station and harbour endpoints, through the real web app."""

import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

import ferrywatch.web as webmod
from ferrywatch.autoroute import AutoRouter
from ferrywatch.harbours import Harbours
from ferrywatch.simsea import fake_overpass_fetch, fake_stations_fetch
from ferrywatch.simulate import fake_chart_layer
from ferrywatch.stations import Stations
from ferrywatch.store import Store
from ferrywatch.vchart import VectorChart


def run_with_client(tmp_path, body):
    async def main():
        store = Store(tmp_path / "t.db")
        chart = VectorChart(tmp_path / "vchart", None, fake_chart_layer)
        fw = SimpleNamespace(store=store, stations=Stations(None, fake_stations_fetch),
                             harbours=Harbours(tmp_path / "harbours.json.gz", None, fake_overpass_fetch),
                             autoroute=AutoRouter(chart))
        client = TestClient(TestServer(webmod.Web(fw).app))
        await client.start_server()
        try:
            await body(client, fw)
        finally:
            await client.close()
            store.close()

    asyncio.run(main())


def test_routes_saved_changed_and_deleted(tmp_path):
    async def body(client, fw):
        assert await (await client.get("/api/routes")).json() == []
        pts = [[59.98, 24.42], [59.95, 24.38], [59.93, 24.30]]
        r = await client.post("/api/routes", json={"name": "  To Rönnskär ", "points": pts, "speed": 6})
        route = await r.json()
        assert route["id"] == 1 and route["name"] == "To Rönnskär" and route["speed"] == 6 and route["shown"]
        assert route["points"] == pts
        second = await (await client.post("/api/routes", json={"points": pts[:2]})).json()
        assert second["id"] == 2 and second["name"] == "Route 2" and second["speed"] == 25
        hidden = await (await client.post("/api/routes", json={"id": 1, "shown": False})).json()
        assert hidden["shown"] is False and hidden["points"] == pts and hidden["name"] == "To Rönnskär"
        assert [r["id"] for r in await (await client.get("/api/routes")).json()] == [1, 2]
        for bad in ({"points": [[59.9, 24.4]]}, {"points": [[95, 24], [59, 24]]}, {"points": pts, "speed": 0},
                    {"points": [[60, 24]] * 201}, {"name": "x"}, [1, 2]):
            assert (await client.post("/api/routes", json=bad)).status == 400, bad
        assert (await client.post("/api/routes", json={"id": 9, "shown": True})).status == 404
        assert (await client.delete("/api/routes/1")).status == 200
        assert (await client.delete("/api/routes/1")).status == 404
        assert [r["id"] for r in fw.store.kv_get("routes")] == [2]

    run_with_client(tmp_path, body)


def test_stations_and_history(tmp_path):
    async def body(client, fw):
        data = await (await client.get("/api/stations")).json()
        assert data["error"] is None and len(data["stations"]) > 15
        mak = next(s for s in data["stations"] if s["id"] == 100997)
        assert {"ws", "wg", "wd", "t", "ts", "lat", "lon", "name"} <= set(mak)
        hist = await (await client.get("/api/stations/100997/history")).json()
        assert len(hist["points"]) >= 24 and len(hist["points"][0]) == 3
        assert (await client.get("/api/stations/1/history")).status == 404
        assert (await client.get("/api/stations/x/history")).status == 404

    run_with_client(tmp_path, body)


def test_harbours_in_view(tmp_path):
    async def body(client, fw):
        first = await (await client.get("/api/harbours?bbox=24.3,59.9,24.6,60.1")).json()
        assert first["pending"]
        await fw.harbours._task
        data = await (await client.get("/api/harbours?bbox=24.3,59.9,24.6,60.1")).json()
        assert not data["pending"] and len(data["harbours"]) == 4
        assert (await client.get("/api/harbours")).status == 400
        assert (await client.get("/api/harbours?bbox=19,59,30,66")).status == 400  # too big

    run_with_client(tmp_path, body)


def test_autoroute_endpoint(tmp_path):
    async def body(client, fw):
        url = "/api/autoroute?from=59.972,24.60&to=59.946,24.40&draught=1.5"
        first = await (await client.get(url)).json()
        assert first["downloading"] > 0
        await fw.autoroute._downloading
        data = await (await client.get(url)).json()
        assert "error" not in data and data["points"][0] == [59.972, 24.6] and data["points"][-1] == [59.946, 24.4]
        for bad in ("from=1&to=2,3", "from=59,24&to=60,25&draught=x", "from=59,24&to=60,25&draught=20", "to=60,25"):
            assert (await client.get(f"/api/autoroute?{bad}")).status == 400, bad

    run_with_client(tmp_path, body)


def test_passages_filtered_paged_and_as_csv(tmp_path):
    async def body(client, fw):
        for i, (mmsi, direction) in enumerate([(1, "east"), (2, "west"), (1, "west"), (2, "east"), (1, "east")]):
            fw.store.add_passage({"mmsi": mmsi, "ts": 1000 + i * 100, "route": "inner", "direction": direction,
                                  "zone_id": None, "lat": 60.0, "lon": 24.4, "sog": 18.0})

        async def ids(query):
            return [(p["mmsi"], p["ts"]) for p in await (await client.get(f"/api/passages?{query}")).json()]

        assert await ids("mmsi=1") == [(1, 1400), (1, 1200), (1, 1000)]
        assert await ids("mmsi=1,2&direction=east") == [(1, 1400), (2, 1300), (1, 1000)]
        assert await ids("since=1100&until=1400") == [(2, 1300), (1, 1200), (2, 1100)]
        assert await ids("limit=2&offset=2") == [(1, 1200), (2, 1100)]
        assert (await client.get("/api/passages?mmsi=x")).status == 400
        csv = await (await client.get("/api/passages.csv?mmsi=2")).text()
        assert len(csv.strip().splitlines()) == 3  # the header and ship 2's two

    run_with_client(tmp_path, body)
