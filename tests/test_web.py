import asyncio
from types import SimpleNamespace

import ferrywatch.web as webmod
from ferrywatch.store import Store
from ferrywatch.tracker import Tracker


def make_web(tmp_path):
    store = Store(tmp_path / "t.db")
    fleet = Tracker(store, members=lambda: {7, 8})
    fleet.load()
    fw = SimpleNamespace(store=store, fleet_tracker=fleet,
                         fleet_summary=lambda mmsi: {"mmsi": mmsi, **(fleet.ship_summary(mmsi))})
    w = webmod.Web(fw)
    sent = []
    w._send = sent.append
    w.sockets = {object()}  # a panel is open
    return w, fleet, sent


def test_fleet_positions_are_throttled(tmp_path, monkeypatch):
    monkeypatch.setattr(webmod, "FLEET_PUSH_SECONDS", 0.2)
    w, fleet, sent = make_web(tmp_path)

    async def run():
        fleet.subscribe(w.on_fleet_event)
        for i in range(5):  # five reports in quick succession
            fleet.on_location(7, {"time": 1000 + i, "lat": 60.0, "lon": 24.0 + i * 0.01, "sog": 20.0})
        fleet.on_location(8, {"time": 1000, "lat": 60.1, "lon": 24.0, "sog": 20.0})
        assert [m["ship"]["mmsi"] for m in sent] == [7, 8]  # one each straight away
        await asyncio.sleep(0.3)

    asyncio.run(run())
    # ...and the latest of the held-back reports once the time was up.
    assert [m["ship"]["mmsi"] for m in sent] == [7, 8, 7]
    assert sent[-1]["ship"]["position"]["ts"] == 1004


def test_no_pushes_without_a_panel(tmp_path):
    w, fleet, sent = make_web(tmp_path)
    w.sockets = set()
    fleet.subscribe(w.on_fleet_event)
    fleet.on_location(7, {"time": 1000, "lat": 60.0, "lon": 24.0, "sog": 20.0})
    assert sent == []
