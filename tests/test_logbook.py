import io
import math
import zipfile
from datetime import datetime, timezone

from ferrywatch.logbook import Logbook, parse_gpx, split_trips
from ferrywatch.web import _read_logbook_file

T0 = int(datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc).timestamp())


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def leg(t, lat, lon, heading_deg, knots, seconds, step=10, depth=None, wtemp=None):
    """Points every `step` seconds going `heading_deg` at `knots`."""
    pts = []
    v = knots * 1852 / 3600
    for k in range(0, seconds + 1, step):
        d = v * k
        la = lat + d * math.cos(math.radians(heading_deg)) / 111320
        lo = lon + d * math.sin(math.radians(heading_deg)) / (111320 * math.cos(math.radians(lat)))
        pts.append((t + k, la, lo, depth, wtemp))
    return pts


def gpx(points, untimed=None):
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx xmlns="http://www.topografix.com/GPX/1/1" '
           'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v2" '
           'xmlns:gpxx="http://www.garmin.com/xmlschemas/GpxExtensions/v3" creator="GPSMAP 92sv" version="1.1">',
           '<wpt lat="60.1" lon="24.4"><name>Fish</name></wpt>',
           '<trk><name>Active Log</name><trkseg>']
    for ts, lat, lon, depth, wtemp in points:
        ext = ""
        if depth is not None or wtemp is not None:
            ext = "<extensions><gpxtpx:TrackPointExtension>"
            if wtemp is not None:
                ext += f"<gpxtpx:wtemp>{wtemp}</gpxtpx:wtemp>"
            if depth is not None:
                ext += f"<gpxtpx:depth>{depth}</gpxtpx:depth>"
            ext += "</gpxtpx:TrackPointExtension></extensions>"
        out.append(f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>0</ele><time>{iso(ts)}</time>{ext}</trkpt>')
    out.append("</trkseg></trk>")
    if untimed:
        out.append("<trk><name>Saved 001</name><trkseg>")
        out += [f'<trkpt lat="{la}" lon="{lo}"></trkpt>' for la, lo in untimed]
        out.append("</trkseg></trk>")
    out.append("</gpx>")
    return "\n".join(out).encode()


def day():
    """Out from Kantvik at 15 kn for 30 min, an hour moored (plotter on, GPS drifting
    a few metres), back for 30 min; then the plotter off for the night and a short
    morning trip with a GPS jump in it."""
    a = leg(T0, 60.08, 24.38, 200, 15, 1800, depth=12.5, wtemp=17.2)
    end = a[-1]
    moored = [(end[0] + 30 * k, end[1] + 0.00002 * (k % 3), end[2], 3.1, 17.5) for k in range(1, 121)]
    b = leg(moored[-1][0] + 30, end[1], end[2], 20, 14, 1800, depth=8.0, wtemp=17.8)
    c = leg(T0 + 86400, 60.08, 24.38, 90, 6, 900)
    c.insert(20, (c[19][0] + 5, 61.0, 26.0, None, None))  # a jump of 100 km
    c = sorted(c)
    return a + moored + b + c


def test_parse_reads_garmin_extensions_and_skips_waypoints():
    parsed = parse_gpx(io.BytesIO(gpx(day(), untimed=[(60.0, 24.0), (60.01, 24.0), (60.02, 24.01)])))
    assert parsed["tracks"] == 2
    first = parsed["timed"][0]
    assert first[0] == T0 and first[4] == 12.5 and first[5] == 17.2
    assert len(parsed["untimed"]) == 1 and parsed["untimed"][0]["name"] == "Saved 001"


def test_split_at_mooring_and_overnight_gap():
    pts = [p[:3] + (None,) + p[3:] for p in day()]
    trips = split_trips(sorted(pts))
    assert len(trips) == 3
    assert trips[0][0][0] == T0
    assert trips[2][0][0] == T0 + 86400
    assert all(abs(p[1] - 61.0) > 0.5 for p in trips[2])  # jump dropped


def test_logbook_upload_twice_adds_nothing(tmp_path):
    book = Logbook(tmp_path / "logbook.db")
    data = gpx(day())
    first = book.add_gpx(io.BytesIO(data), "Track.gpx")
    assert first["new_trips"] == 3 and first["depth"] and first["wtemp"]
    again = book.add_gpx(io.BytesIO(data), "Track.gpx")
    assert again["new_points"] == 0 and again["new_trips"] == 0
    listing = book.listing()
    trips = listing["trips"]
    assert [t["start"] for t in trips] == sorted((t["start"] for t in trips), reverse=True)
    out = next(t for t in trips if t["start"] == T0)
    assert abs(out["distance_nm"] - 7.5) < 0.2       # 30 min at 15 kn
    assert abs(out["avg_kn"] - 15) < 0.5
    assert abs(out["top_kn"] - 15) < 0.5
    assert out["depth"]["min"] == 12.5 and out["wtemp"]["avg"] == 17.2
    assert listing["years"]["2026"]["trips"] == 3
    detail = book.trip(out["id"])
    assert detail["points"][0][:3] == [60.08, 24.38, T0]
    assert abs(detail["points"][0][3] - 15) < 0.5


def test_partial_card_then_whole_card_makes_one_trip(tmp_path):
    book = Logbook(tmp_path / "logbook.db")
    a = leg(T0, 60.08, 24.38, 200, 15, 1800)
    book.add_gpx(io.BytesIO(gpx(a[:90])))
    book.add_gpx(io.BytesIO(gpx(a)))
    trips = book.listing()["trips"]
    assert len(trips) == 1 and abs(trips[0]["distance_nm"] - 7.5) < 0.2


def test_names_notes_and_delete_stays_deleted(tmp_path):
    book = Logbook(tmp_path / "logbook.db")
    data = gpx(day(), untimed=[(60.0, 24.0), (60.01, 24.0), (60.02, 24.01)])
    book.add_gpx(io.BytesIO(data))
    trips = book.listing()["trips"]
    assert len(trips) == 4 and trips[-1]["timed"] is False
    tid = trips[0]["id"]
    assert book.set_info(tid, " Morning run ", "Calm")
    assert book.listing()["trips"][0]["name"] == "Morning run"
    assert book.delete(tid)
    book.add_gpx(io.BytesIO(data))  # the same card again
    ids = [t["id"] for t in book.listing()["trips"]]
    assert tid not in ids and len(ids) == 3


def test_zip_and_unreadable_files(tmp_path):
    book = Logbook(tmp_path / "logbook.db")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Garmin/GPX/Current.gpx", gpx(day()))
        z.writestr("Garmin/readme.txt", "x")
    buf.seek(0)
    res = _read_logbook_file(book, buf, "card.zip")
    assert len(res) == 1 and res[0]["new_trips"] == 3
    assert "GPX" in _read_logbook_file(book, io.BytesIO(b"\x00\x01"), "USERDATA.ADM")[0]["error"]
    assert "error" in _read_logbook_file(book, io.BytesIO(b"not xml"), "x.gpx")[0]
