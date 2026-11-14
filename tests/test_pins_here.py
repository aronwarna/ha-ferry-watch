from pathlib import Path

import pytest

from ferrywatch import here, pins


def test_parse_coords_formats():
    assert pins.parse_coords("60.1234 24.5678") == pytest.approx((60.1234, 24.5678))
    lat, lon = pins.parse_coords("60°07.2'N 24°27'E")
    assert lat == pytest.approx(60.12) and lon == pytest.approx(24.45)
    lat, lon = pins.parse_coords("59 57 0 N 24 25 30 E")
    assert lat == pytest.approx(59.95) and lon == pytest.approx(24.425)
    assert pins.parse_coords("Rönnskär") is None


def test_clean_pin_and_gpx():
    p = pins.clean_pin({"lat": "60.1", "lon": 24.4, "name": " Perch <spot> ", "kind": "nope", "note": "6 m & rocks"})
    assert p["kind"] == "pin" and p["name"] == "Perch <spot>" and p["id"]
    edited = pins.clean_pin({"name": "B", "kind": "fish"}, p)
    assert edited["id"] == p["id"] and edited["lat"] == 60.1 and edited["kind"] == "fish"
    with pytest.raises(ValueError):
        pins.clean_pin({"lat": 95, "lon": 0})
    gpx = pins.pins_gpx([p])
    assert "&lt;spot&gt;" in gpx and "6 m &amp; rocks" in gpx and 'lat="60.1"' in gpx


def test_depth_at_without_block(tmp_path: Path):
    class FakeChart:
        def _path(self, bx, by):
            return tmp_path / f"{bx}_{by}.blk"
    assert here.depth_at(FakeChart(), 59.95, 24.4) is None


def test_gpx_waypoints():
    text = """<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1">
    <wpt lat="60.1" lon="24.5"><name>Hauki</name><sym>Fishing Hot Spot Facility</sym><desc>deep</desc></wpt>
    <wpt lat="60.2" lon="24.6"><name>Yö</name><sym>Anchor</sym></wpt>
    <wpt lat="60.3" lon="24.7"><name>X</name><sym>Flag, Blue</sym></wpt></gpx>"""
    rows = pins.gpx_waypoints(text)
    assert [r["kind"] for r in rows] == ["fish", "anchor", "pin"] and rows[0]["note"] == "deep"
    with pytest.raises(ValueError):
        pins.gpx_waypoints('<!DOCTYPE x [<!ENTITY a "b">]><gpx/>')
