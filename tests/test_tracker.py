import time
from datetime import datetime, timedelta, timezone

from ferrywatch.store import Store
from ferrywatch.tracker import PORT_STOP_SECONDS, Tracker, decode_eta, parse_location

MMSI = 230184000
HELSINKI = (60.160, 24.956)


def make(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_ship(MMSI, "Silja Serenade", "#000")
    tracker = Tracker(store)
    tracker.load()
    return store, tracker


def loc(ts, lat=HELSINKI[0], lon=HELSINKI[1], sog=0.0, cog=90.0):
    return {"time": ts, "lat": lat, "lon": lon, "sog": sog, "cog": cog, "heading": int(cog)}


def test_parse_rejects_unavailable_position():
    assert parse_location(MMSI, {"time": 1, "lat": 91, "lon": 181}) is None
    assert parse_location(MMSI, {"lat": 60, "lon": 24}) is None
    p = parse_location(MMSI, {"time": 1, "lat": 60, "lon": 24, "sog": 102.3, "cog": 360, "heading": 511})
    assert p.sog is None and p.cog is None and p.heading is None


def test_port_stop_then_departure_starts_voyage(tmp_path):
    store, tracker = make(tmp_path)
    t = 1_000_000
    for i in range(0, PORT_STOP_SECONDS + 60, 60):
        tracker.on_location(MMSI, loc(t + i))
    assert tracker.ships[MMSI].in_port
    assert tracker.ships[MMSI].port == "Helsinki"
    depart = t + PORT_STOP_SECONDS + 600
    tracker.on_location(MMSI, loc(depart, lat=60.15, sog=12.0))
    voyage = store.current_voyage(MMSI)
    assert voyage == {"id": voyage["id"], "mmsi": MMSI, "start_ts": depart, "from_port": "Helsinki"}
    assert tracker.voyage_start(MMSI) == depart
    assert not tracker.ships[MMSI].in_port


def test_short_stop_does_not_split_voyage(tmp_path):
    store, tracker = make(tmp_path)
    t = 1_000_000
    tracker.on_location(MMSI, loc(t, sog=15))
    first = tracker.voyage_start(MMSI)
    for i in range(1, 10):  # 9 minutes stopped, e.g. waiting for traffic
        tracker.on_location(MMSI, loc(t + i * 60, sog=0.2))
    tracker.on_location(MMSI, loc(t + 700, sog=15))
    assert tracker.voyage_start(MMSI) == first


def test_thinning_stores_every_30s_while_moving(tmp_path):
    store, tracker = make(tmp_path)
    t = 1_000_000
    for i in range(0, 300, 5):  # a message every 5 s for 5 minutes, straight line
        tracker.on_location(MMSI, loc(t + i, lat=60 + i * 1e-5, sog=15))
    assert store.position_count() == 10


def test_turn_is_stored_early(tmp_path):
    store, tracker = make(tmp_path)
    t = 1_000_000
    tracker.on_location(MMSI, loc(t, sog=15, cog=90))
    tracker.on_location(MMSI, loc(t + 10, sog=15, cog=95))
    tracker.on_location(MMSI, loc(t + 20, sog=15, cog=120))
    assert store.position_count() == 2


def test_out_of_order_messages_are_ignored(tmp_path):
    store, tracker = make(tmp_path)
    tracker.on_location(MMSI, loc(2000, sog=15))
    tracker.on_location(MMSI, loc(1000, sog=15))
    assert tracker.ships[MMSI].last.ts == 2000


def test_unknown_ship_is_ignored(tmp_path):
    store, tracker = make(tmp_path)
    tracker.on_location(123456789, loc(1000))
    assert store.position_count() == 0


def test_port_state_survives_restart(tmp_path):
    store, tracker = make(tmp_path)
    t = 1_000_000
    for i in range(0, PORT_STOP_SECONDS + 600, 60):
        tracker.on_location(MMSI, loc(t + i))
    restarted = Tracker(store)
    restarted.load()
    assert restarted.ships[MMSI].in_port
    restarted.on_location(MMSI, loc(t + PORT_STOP_SECONDS + 900, sog=10))
    assert store.current_voyage(MMSI)["from_port"] == "Helsinki"


def test_listeners_receive_summary(tmp_path):
    store, tracker = make(tmp_path)
    events = []
    tracker.subscribe(lambda kind, payload: events.append((kind, payload)))
    tracker.on_location(MMSI, loc(1000, sog=15))
    tracker.on_metadata(MMSI, {"destination": "STOCKHOLM@@", "timestamp": 1_000_000})
    assert events[0][0] == "position" and events[0][1]["position"]["sog"] == 15
    assert events[1][1]["destination"] == "STOCKHOLM"


def test_track_returns_since_voyage(tmp_path):
    store, tracker = make(tmp_path)
    for i in range(0, 600, 30):
        tracker.on_location(MMSI, loc(1000 + i, sog=15))
    pts = store.track(MMSI, tracker.voyage_start(MMSI))
    assert len(pts) == 20 and pts[0][2] == 1000


def test_stopped_at_a_port_counts_as_in_port_right_away(tmp_path):
    from ferrywatch.store import Store
    from ferrywatch.tracker import Tracker
    store = Store(tmp_path / "t.db")
    store.add_ship(1, "Ship", "#111111")
    tracker = Tracker(store)
    tracker.load()
    # One report alongside in Helsinki South Harbour, then silence.
    tracker.on_location(1, {"time": 1000, "lat": 60.1655, "lon": 24.9555, "sog": 0.0, "cog": 0, "heading": 0})
    summary = tracker.ship_summary(1)
    assert summary["in_port"] and summary["port"] == "Helsinki"
    assert summary["stopped_since"] == 1000


def _packed(month, day, hour, minute):
    return month << 16 | day << 11 | hour << 6 | minute


def test_decode_eta_takes_the_nearest_year():
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc).timestamp()
    assert decode_eta(_packed(10, 20, 7, 45), now) == datetime(2026, 10, 20, 7, 45, tzinfo=timezone.utc).timestamp()
    # Early January seen in late December belongs to next year, and the other way round.
    dec = datetime(2026, 12, 30, tzinfo=timezone.utc).timestamp()
    assert decode_eta(_packed(1, 2, 6, 0), dec) == datetime(2027, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
    jan = datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()
    assert decode_eta(_packed(12, 31, 22, 0), jan) == datetime(2026, 12, 31, 22, 0, tzinfo=timezone.utc).timestamp()
    # "Not available" (month 0, day 0, hour 24, minute 60) and impossible dates.
    assert decode_eta(_packed(0, 0, 24, 60), now) is None
    assert decode_eta(0, now) is None
    assert decode_eta(_packed(2, 30, 10, 0), now) is None
    assert decode_eta(None, now) is None


def test_metadata_eta_and_draught_reach_the_summary(tmp_path):
    store, tracker = make(tmp_path)
    soon = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(hours=5)
    tracker.on_metadata(MMSI, {"name": "SILJA SERENADE", "destination": "SE STO", "draught": 71,
                               "eta": _packed(soon.month, soon.day, soon.hour, soon.minute),
                               "timestamp": int(time.time() * 1000)})
    summary = tracker.ship_summary(MMSI)
    assert summary["eta_ais"] == int(soon.timestamp())
    assert summary["draught"] == 7.1
    assert store.meta(MMSI)["eta_ts"] == int(soon.timestamp())
    # A missing draught or ETA keeps the last one.
    tracker.on_metadata(MMSI, {"draught": 0, "eta": _packed(0, 0, 24, 60), "timestamp": int(time.time() * 1000)})
    assert tracker.ship_summary(MMSI)["draught"] == 7.1


def test_old_eta_is_not_shown(tmp_path):
    store, tracker = make(tmp_path)
    tracker.ships[MMSI].meta["eta_ts"] = int(time.time()) - 3 * 3600
    assert tracker.ship_summary(MMSI)["eta_ais"] is None
