from ferrywatch.ais import rest_feature_to_location


def test_rest_feature_conversion():
    feature = {
        "mmsi": 230184000,
        "geometry": {"type": "Point", "coordinates": [24.95, 60.16]},
        "properties": {"sog": 0.1, "cog": 12.3, "heading": 200, "timestampExternal": 1_700_000_000_123},
    }
    assert rest_feature_to_location(feature) == {
        "lat": 60.16, "lon": 24.95, "time": 1_700_000_000, "sog": 0.1, "cog": 12.3, "heading": 200,
    }


def test_rest_feature_without_geometry():
    assert rest_feature_to_location({"properties": {}}) is None


def test_mqtt_message_reaches_tracker(tmp_path):
    import asyncio
    import json

    import paho.mqtt.client as mqtt

    from ferrywatch.ais import DigitrafficFeed
    from ferrywatch.store import Store
    from ferrywatch.tracker import Tracker

    async def run():
        store = Store(tmp_path / "t.db")
        store.add_ship(230184000, "Silja Serenade", "#000")
        tracker = Tracker(store)
        tracker.load()
        feed = DigitrafficFeed(asyncio.get_running_loop(), None, store, tracker)
        for topic, payload in [
            ("vessels-v2/230184000/location", {"time": 1000, "lat": 60.1, "lon": 24.9, "sog": 15.0, "cog": 250.0}),
            ("vessels-v2/230184000/metadata", {"destination": "STOCKHOLM", "timestamp": 1_000_000}),
            ("vessels-v2/230184000/locations", {"time": 1060, "lat": 60.09, "lon": 24.8, "sog": 15.0}),
            ("vessels-v2/status", {"updated": 1}),
        ]:
            msg = mqtt.MQTTMessage(topic=topic.encode())
            msg.payload = json.dumps(payload).encode()
            feed._on_message(None, None, msg)
        await asyncio.sleep(0)
        return tracker, feed

    tracker, feed = asyncio.run(run())
    summary = tracker.ship_summary(230184000)
    assert summary["position"]["ts"] == 1060
    assert summary["destination"] == "STOCKHOLM"
    assert feed.message_count == 3


VESSEL_LIST = """[
  {"mmsi": 230184000, "name": "SILJA SERENADE", "shipType": 60, "referencePointA": 160, "referencePointB": 43,
   "destination": "SESTO>FIHEL", "timestamp": 1700000000000},
  {"mmsi": 265004000, "name": "SILJA SYMPHONY", "shipType": 60, "destination": "", "timestamp": 1700000000000},
  {"mmsi": 230000001, "name": "SILJA@@@@", "shipType": 37, "timestamp": 1700000000000},
  {"mmsi": 276000101, "name": "MEGASTAR", "shipType": 69, "timestamp": 1700000000000},
  {"mmsi": 230000002, "name": "UTÖ", "shipType": 60, "timestamp": 1700000000000},
  {"name": "NO MMSI"}
]"""


def make_feed(store, text, tracker=None):
    from ferrywatch.ais import DigitrafficFeed
    from ferrywatch.tracker import Tracker

    feed = DigitrafficFeed(None, None, store, tracker or Tracker(store))
    feed.vessel_list_source = lambda: text
    return feed


def test_iter_json_array():
    from ferrywatch.ais import iter_json_array
    assert list(iter_json_array(' [ {"a": 1} , {"b": [2, 3]},{"c":"]"} ] ')) == [{"a": 1}, {"b": [2, 3]}, {"c": "]"}]
    assert list(iter_json_array("[]")) == []


def test_vessel_directory_and_search(tmp_path):
    import asyncio

    from ferrywatch.fleets import FleetMatcher
    from ferrywatch.store import Store

    store = Store(tmp_path / "t.db")
    feed = make_feed(store, VESSEL_LIST)
    matcher = FleetMatcher(now_ms=1700000000000)
    count = asyncio.run(feed.refresh_directory(matcher))
    assert count == 5 and store.vessel_count() == 5
    assert matcher.result()[276000101] == {"fleet": "tallink", "name": "Megastar"}
    assert [r["mmsi"] for r in feed.search("silja")] == [230000001, 230184000, 265004000]
    assert feed.search("silja serenade")[0] == {
        "mmsi": 230184000, "name": "Silja Serenade", "destination": "SESTO>FIHEL", "ship_type": 60, "length": 203}
    assert [r["mmsi"] for r in feed.search("2650")] == [265004000]
    assert [r["mmsi"] for r in feed.search("utö")] == [230000002]  # accents fold like AIS names
    assert feed.search("100%") == [] and feed.search("") == []
    assert store.vessels_named("SILJA SYMPHONY") == [265004000]
    assert store.vessel_info([230184000, 1])[230184000]["length"] == 203


def test_bad_vessel_list_keeps_directory(tmp_path):
    import asyncio

    import pytest

    from ferrywatch.store import Store

    store = Store(tmp_path / "t.db")
    asyncio.run(make_feed(store, VESSEL_LIST).refresh_directory())
    with pytest.raises(ValueError):  # cut off half way
        asyncio.run(make_feed(store, '[{"mmsi": 1, "name": "A"}, {"mmsi": 2, ').refresh_directory())
    assert store.vessel_count() == 5


def test_reflagged_ship_found_in_directory(tmp_path):
    import asyncio

    from ferrywatch.store import Store
    from ferrywatch.tracker import Tracker

    store = Store(tmp_path / "t.db")
    store.add_ship(265999999, "Silja Symphony", "#000", auto_resolve=True)
    tracker = Tracker(store)
    tracker.load()
    feed = make_feed(store, VESSEL_LIST, tracker)

    async def no_positions(url, params=None):
        return {"features": []}
    feed._get_json = no_positions
    asyncio.run(feed.resolve_reflagged())
    assert store.ship(265004000) and not store.ship(265999999)
    assert set(tracker.ships) == {265004000}
