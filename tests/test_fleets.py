import asyncio
import time

from ferrywatch.ais import DigitrafficFeed
from ferrywatch.fleets import FleetManager, display_name, match_fleets
from ferrywatch.store import Position, Store
from ferrywatch.tracker import Tracker

NOW_MS = int(time.time() * 1000)


def vessel(mmsi, name, ship_type=60, age_days=0):
    return {"mmsi": mmsi, "name": name, "shipType": ship_type, "timestamp": NOW_MS - age_days * 86_400_000}


def test_match_fleets():
    members = match_fleets([
        vessel(1, "MEGASTAR"),
        vessel(2, "VIKING XPRS"),
        vessel(3, "FINNSTAR", ship_type=69),
        vessel(4, "FINNTUG", ship_type=52),  # a tug, not a Finnlines ship
        vessel(5, "VIKING SKY"),  # Viking cruise ship, not Viking Line
        vessel(6, "GABRIELLA@@@"),
        vessel(7, "MYSTAR"),
    ])
    assert members == {
        1: {"fleet": "tallink", "name": "Megastar"},
        2: {"fleet": "viking", "name": "Viking XPRS"},
        3: {"fleet": "finnlines", "name": "Finnstar"},
        6: {"fleet": "viking", "name": "Gabriella"},
        7: {"fleet": "tallink", "name": "MyStar"},
    }


def test_reflagged_name_keeps_newest_mmsi():
    members = match_fleets([vessel(10, "BALTIC QUEEN", age_days=400), vessel(11, "BALTIC QUEEN")])
    assert list(members) == [11]


def test_display_name():
    assert display_name("SILJA SERENADE") == "Silja Serenade"
    assert display_name("VICTORIA I") == "Victoria I"


def test_fleet_manager(tmp_path):
    store = Store(tmp_path / "t.db")
    fm = FleetManager(store)
    assert fm.update({1: {"fleet": "tallink", "name": "Megastar"}, 2: {"fleet": "finnlines", "name": "Finnstar"},
                      3: {"fleet": "tallink", "name": "Silja Serenade"}})
    assert set(fm.active(tracked={3})) == {1, 2}  # my own ships aren't followed twice
    fm.set_enabled("finnlines", False)
    assert set(fm.active(tracked=set())) == {1, 3}
    assert not fm.update({})  # an empty vessel list doesn't wipe the fleets

    again = FleetManager(store)  # survives a restart
    assert again.enabled == {"tallink", "viking", "finferries", "followed"}
    assert set(again.members) == {1, 2, 3}
    summary = {f["id"]: f for f in again.summary()}
    assert summary["tallink"]["members"] == [1, 3] and not summary["finnlines"]["enabled"]


def test_fleet_tracker_stores_sparser(tmp_path):
    store = Store(tmp_path / "t.db")
    tracker = Tracker(store, members=lambda: {5}, store_moving=60, store_stopped=600)
    tracker.load()
    assert set(tracker.ships) == {5}
    for i in range(10):  # a report every 10 s for 100 s
        tracker.on_location(5, {"time": 1000 + i * 10, "lat": 60.0, "lon": 24.0 + i * 0.001,
                                "sog": 20.0, "cog": 90.0, "heading": 90})
    assert len(store.track(5, 0)) == 2  # at 1000 and 1060


def test_feed_dispatches_to_both_trackers(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_ship(1, "Mine", "#111111")
    main = Tracker(store)
    main.load()
    fleet = Tracker(store, members=lambda: {2})
    fleet.load()

    async def run():
        feed = DigitrafficFeed(asyncio.get_running_loop(), None, store, main)
        feed.add_tracker(fleet)
        assert feed.wanted() == {1, 2}
        msg = {"time": 1000, "lat": 60.0, "lon": 24.0, "sog": 10.0, "cog": 90.0, "heading": 90}
        feed._dispatch(1, "location", msg)
        feed._dispatch(2, "location", msg)
        feed._dispatch(3, "location", msg)  # nobody follows it
        return feed
    asyncio.run(run())
    assert main.ships[1].last and fleet.ships[2].last
    assert 2 not in main.ships and store.track(3, 0) == []


def test_prune_untracked(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_ship(1, "Mine", "#111111")
    for mmsi in (1, 2):
        store.insert_position(Position(mmsi=mmsi, ts=100, lat=60, lon=24, sog=10, cog=0, heading=0))
        store.insert_position(Position(mmsi=mmsi, ts=900, lat=60, lon=24, sog=10, cog=0, heading=0))
    assert store.prune_untracked(500) == 1
    assert len(store.track(1, 0)) == 2 and len(store.track(2, 0)) == 1


def test_finferries_need_a_ship_type():
    members = match_fleets([
        vessel(30, "UTÖ", ship_type=60), vessel(31, "STELLA", ship_type=37), vessel(32, "TUULI", ship_type=69),
    ])
    assert members == {30: {"fleet": "finferries", "name": "Utö"}, 32: {"fleet": "finferries", "name": "Tuuli"}}


def test_followed_ships_and_new_fleets(tmp_path):
    store = Store(tmp_path / "t.db")
    store.kv_set("fleets_enabled", ["tallink"])  # saved by an older version
    fm = FleetManager(store)
    assert fm.enabled == {"tallink", "finferries", "followed"}  # fleets new in this version start on
    assert fm.updated_ts == 0  # changed fleet lists are matched again right away
    fm.follow(40, "Aranda")
    fm.update({1: {"fleet": "tallink", "name": "Megastar"}})
    again = FleetManager(store)
    assert again.members[40] == {"fleet": "followed", "name": "Aranda"}
    assert set(again.active(tracked=set())) == {1, 40}
    again.unfollow(40)
    assert 40 not in FleetManager(store).members
