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
