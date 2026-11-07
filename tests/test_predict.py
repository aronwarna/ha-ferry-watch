import asyncio

from ferrywatch.geo import distance_m
from ferrywatch.ha import NEXT_PASSAGE_ENTITY, NextPassageSensor
from ferrywatch.predict import Predictor, inner_habit, predict
from ferrywatch.simulate import FakeHA
from ferrywatch.store import Position, Store

MMSI = 230184000
START = (60.10, 24.90)  # off Helsinki
LINE = (59.99, 24.36)  # the Porkkala line
T0 = 1_700_000_000
STEPS = 60  # one point a minute for an hour


def along(f):
    return START[0] + (LINE[0] - START[0]) * f, START[1] + (LINE[1] - START[1]) * f


def make_store(tmp_path):
    """An earlier westbound passage, its last hour of track a straight line to the Porkkala line."""
    store = Store(tmp_path / "t.db")
    store.add_ship(MMSI, "Silja Serenade", "#000")
    knots = distance_m(*START, *LINE) / (STEPS * 60) / 0.514444
    for i in range(STEPS + 1):
        lat, lon = along(i / STEPS)
        store.insert_position(Position(mmsi=MMSI, ts=T0 + i * 60, lat=lat, lon=lon, sog=knots, cog=250.0, heading=250))
    store.add_passage({"mmsi": MMSI, "ts": T0 + STEPS * 60, "route": "inner", "direction": "west", "zone_id": 1,
                       "lat": LINE[0], "lon": LINE[1], "sog": knots})
    return store, knots


def test_eta_from_an_earlier_track(tmp_path):
    store, knots = make_store(tmp_path)
    now = T0 + 86400
    lat, lon = along(0.5)  # half way, so half an hour to go at the same speed
    pos = {"lat": lat, "lon": lon, "sog": knots, "cog": 248.0, "heading": 250, "ts": now}
    result = predict(store, MMSI, pos, now)
    assert result["direction"] == "west"
    assert abs(result["eta_ts"] - (now + 30 * 60)) <= 60
    # Sailing slower than the earlier ship takes longer.
    slow = predict(store, MMSI, {**pos, "sog": knots / 1.25}, now)
    assert abs(slow["eta_ts"] - (now + 30 * 60 * 1.25)) <= 90


def test_no_eta_when_it_does_not_fit(tmp_path):
    store, knots = make_store(tmp_path)
    lat, lon = along(0.5)
    pos = {"lat": lat, "lon": lon, "sog": knots, "cog": 70.0, "heading": 70, "ts": T0 + 86400}
    assert predict(store, MMSI, pos) is None  # heading the other way
    assert predict(store, MMSI, {**pos, "cog": 250.0, "lat": lat + 0.1}) is None  # 11 km off the track
    assert predict(store, MMSI, {**pos, "cog": 250.0, "sog": 2.0}) is None  # barely moving


def test_inner_habit(tmp_path):
    store, _ = make_store(tmp_path)
    now = T0 + 86400
    for day in range(3):
        store.start_voyage(MMSI, now - day * 86400 - 3600, "Helsinki")
    store.start_voyage(MMSI, now - 3600 * 12, "Stockholm")
    assert inner_habit(store, MMSI, "west", now) == {"inner": 1, "of": 3}
    assert inner_habit(store, MMSI, "east", now) == {"inner": 0, "of": 3}
    assert inner_habit(store, 1, "west", now) == {}


def test_predictor_recomputes_once_a_minute(tmp_path):
    store, knots = make_store(tmp_path)
    changes = []
    predictor = Predictor(store, lambda: changes.append(1))
    lat, lon = along(0.5)
    import time
    now = int(time.time())
    event = {"mmsi": MMSI, "in_port": False,
             "position": {"lat": lat, "lon": lon, "sog": knots, "cog": 250.0, "heading": 250, "ts": now}}
    predictor.on_event("position", event)
    first = predictor.get(MMSI)
    assert first and changes == [1]
    predictor.on_event("position", {**event, "in_port": True})  # within the minute: ignored
    assert predictor.get(MMSI) == first
    assert predictor.soonest({MMSI, 1}) == (MMSI, first)
    predictor._computed.clear()
    predictor.on_event("position", {**event, "in_port": True})
    assert predictor.get(MMSI) is None and changes == [1, 1]


def test_next_passage_sensor_sends_only_real_changes():
    ha = FakeHA()
    sensor = NextPassageSensor(ha)

    def update(eta, mmsi=MMSI):
        p = {"eta_ts": eta, "direction": "west", "inner": 9, "of": 14} if eta else None
        return asyncio.run(sensor.update("Silja Serenade", mmsi, p))

    assert update(T0)
    state, attrs = ha.states[NEXT_PASSAGE_ENTITY]
    assert state == "2023-11-14T22:13:20+00:00"
    assert attrs["device_class"] == "timestamp" and attrs["direction"] == "towards Stockholm"
    assert attrs["inner_route"] == 9 and attrs["sailings"] == 14
    assert not update(T0 + 90)  # moved less than two minutes
    assert update(T0 + 200)
    assert update(T0 + 200, mmsi=265004000)  # another ship
    assert update(None) and ha.states[NEXT_PASSAGE_ENTITY][0] == "unknown"
    assert not update(None)
