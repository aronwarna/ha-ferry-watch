from ferrywatch.config import DEFAULT_ZONES
from ferrywatch.geo import line_crossed, point_in_polygon, segments_cross
from ferrywatch.passages import COOLDOWN, Detector
from ferrywatch.store import Position, Store

MMSI = 230184000
# A north-south gate line across the fairway at lon 24.40.
GATE = [[60.05, 24.40], [59.95, 24.40]]


def pos(ts, lat, lon, sog=17.0, cog=270.0):
    return Position(mmsi=MMSI, ts=ts, lat=lat, lon=lon, sog=sog, cog=cog, heading=int(cog))


def make(tmp_path, zones):
    store = Store(tmp_path / "t.db")
    for name, role, shape, points in zones:
        store.add_zone(name, role, shape, points)
    got = []
    return store, Detector(store, got.append), got


def test_segments_cross_basic():
    assert segments_cross((60.0, 24.5), (60.0, 24.3), (60.05, 24.4), (59.95, 24.4))
    assert not segments_cross((60.0, 24.5), (60.0, 24.45), (60.05, 24.4), (59.95, 24.4))
    assert not segments_cross((60.1, 24.5), (60.1, 24.3), (60.05, 24.4), (59.95, 24.4))  # passes north of it


def test_point_on_gate_counts_once():
    a, b, c = (60.0, 24.45), (60.0, 24.40), (60.0, 24.35)
    assert line_crossed(a, b, GATE)  # ends on the line
    assert not line_crossed(b, c, GATE)  # starts on it: already counted


def test_gate_crossing_westbound(tmp_path):
    store, det, got = make(tmp_path, [("Inner", "inner", "line", GATE)])
    det.on_position(pos(1000, 60.0, 24.45))
    det.on_position(pos(1060, 60.0, 24.42))
    assert got == []
    det.on_position(pos(1120, 60.0, 24.36))  # crosses lon 24.40
    assert len(got) == 1
    assert got[0]["route"] == "inner" and got[0]["direction"] == "west" and got[0]["ts"] == 1120
    assert store.passages()[0]["id"] == got[0]["id"]


def test_gate_crossing_eastbound_with_sparse_reports(tmp_path):
    store, det, got = make(tmp_path, [("Inner", "inner", "line", GATE)])
    det.on_position(pos(1000, 60.0, 24.10, cog=80))
    det.on_position(pos(1600, 60.0, 24.70, cog=80))  # 10 min later, far past the gate
    assert got[0]["direction"] == "east"


def test_no_crossing_when_slow_or_gap_too_long(tmp_path):
    store, det, got = make(tmp_path, [("Inner", "inner", "line", GATE)])
    det.on_position(pos(1000, 60.0, 24.45, sog=3))
    det.on_position(pos(1060, 60.0, 24.35, sog=3))  # drifting
    det.on_position(pos(1100, 60.0, 24.33))
    det.on_position(pos(1100 + 3600, 60.0, 24.45))  # back across, but after an hour without data
    assert got == []


def test_cooldown_prevents_double_alerts(tmp_path):
    store, det, got = make(tmp_path, [("Inner", "inner", "line", GATE),
                                      ("Inner 2", "inner", "line", [[60.05, 24.30], [59.95, 24.30]])])
    det.on_position(pos(1000, 60.0, 24.45))
    det.on_position(pos(1060, 60.0, 24.35))
    det.on_position(pos(1120, 60.0, 24.25))  # second inner line within the cooldown
    assert len(got) == 1
    det.on_position(pos(1000 + COOLDOWN + 60, 60.0, 24.45, cog=90))
    det.on_position(pos(1000 + COOLDOWN + 120, 60.0, 24.35, cog=90))
    assert len(got) == 2


def test_outer_gate_is_recorded_too(tmp_path):
    store, det, got = make(tmp_path, [("Outer", "outer", "line", [[59.90, 24.40], [59.80, 24.40]])])
    det.on_position(pos(1000, 59.85, 24.45))
    det.on_position(pos(1060, 59.85, 24.35))
    assert got[0]["route"] == "outer"


def test_area_needs_two_points_inside(tmp_path):
    area = [[60.1, 24.3], [60.1, 24.5], [59.9, 24.5], [59.9, 24.3]]
    store, det, got = make(tmp_path, [("Inner area", "inner", "polygon", area)])
    det.on_position(pos(1000, 60.0, 24.55))
    det.on_position(pos(1060, 60.0, 24.45))  # first point inside
    assert got == []
    det.on_position(pos(1120, 60.0, 24.40))
    assert len(got) == 1 and got[0]["direction"] == "west"


def test_area_corner_clip_does_not_count(tmp_path):
    area = [[60.1, 24.3], [60.1, 24.5], [59.9, 24.5], [59.9, 24.3]]
    store, det, got = make(tmp_path, [("Inner area", "inner", "polygon", area)])
    det.on_position(pos(1000, 60.0, 24.45))
    det.on_position(pos(1060, 60.2, 24.45))  # out again
    det.on_position(pos(1120, 60.0, 24.45))
    assert got == []


def test_phone_zones_are_not_routes(tmp_path):
    store, det, got = make(tmp_path, [("Me", "phone", "polygon", [[60.1, 24.3], [60.1, 24.5], [59.9, 24.4]])])
    det.on_position(pos(1000, 60.0, 24.45))
    det.on_position(pos(1060, 60.0, 24.40))
    assert got == []


def test_default_zones_from_old_setup():
    inner = DEFAULT_ZONES[0]["points"]
    phone = DEFAULT_ZONES[1]["points"]
    assert point_in_polygon(60.09, 24.68, inner)  # inner fairway west of Helsinki
    assert not point_in_polygon(59.95, 24.70, inner)  # outer route
    assert point_in_polygon(60.115, 24.45, phone)  # coast near Porkkala
