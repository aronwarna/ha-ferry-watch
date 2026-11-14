import asyncio
import json
from types import SimpleNamespace

from aiohttp.test_utils import make_mocked_request

import ferrywatch.web as webmod
from ferrywatch.aircraft import AircraftWatcher
from ferrywatch.airhistory import KEEP_DAYS, AircraftHistory, decode, encode, simplify
from ferrywatch.store import Store

T0 = 1_800_000_000


def ac(lat=60.0, lon=24.0, alt_ft=1000, track=90.0, gs=120, **kw):
    return {"hex": "461F01", "reg": "OH-HMV", "callsign": "FINNHEMS10", "type": "H145", "type_name": None,
            "operator": "FinnHEMS", "helicopter": True, "lat": lat, "lon": lon, "alt_ft": alt_ft,
            "track": track, "gs_kn": gs, **kw}


def history(tmp_path):
    return AircraftHistory(Store(tmp_path / "t.db").db)


def points(h):
    return h.db.execute("SELECT ts, alt, trk FROM air_points ORDER BY ts").fetchall()


def test_downsampling(tmp_path):
    h = history(tmp_path)
    fid = h.record(ac(), T0)
    assert h.record(ac(lon=24.001), T0 + 10) == fid  # 10 s later, same heading and height: thinned out
    h.record(ac(lon=24.002), T0 + 30)  # 30 s since the last stored point
    h.record(ac(lon=24.003, track=110), T0 + 40)  # turned 20°
    h.record(ac(lon=24.004, track=112, alt_ft=1500), T0 + 45)  # climbed 150 m
    h.record(ac(lon=24.005, track=112, alt_ft=1550), T0 + 50)  # 15 m and 2°: dropped
    h.record(ac(lon=24.005), T0 + 45)  # out of order: ignored
    assert [p[0] - T0 for p in points(h)] == [0, 30, 40, 45]
    assert points(h)[3][1:] == (457, 112)  # metres and degrees
    flight = h.flight(fid)
    assert flight["points"] == 4 and flight["max_alt"] == 457 and flight["end_ts"] == T0 + 45
    assert flight["reg"] == "OH-HMV" and flight["helicopter"] is True and flight["dist_km"] > 0.1
    assert [p[2] for p in flight["track"]] == [T0, T0 + 30, T0 + 40, T0 + 45]


def test_gap_starts_a_new_flight_and_ground_is_skipped(tmp_path):
    h = history(tmp_path)
    first = h.record(ac(), T0)
    h.record(ac(lon=24.1), T0 + 60)
    assert h.record(ac(alt_ft=0, gs=5), T0 + 120) is None  # landed, standing on the pad
    second = h.record(ac(lon=24.2), T0 + 60 + 21 * 60)
    assert second != first
    assert h.db.execute("SELECT line FROM air_flights WHERE id = ?", (first,)).fetchone()[0]  # finished
    # After a restart the flight carries on if the aircraft reappears within 20 minutes.
    h2 = AircraftHistory(h.db)
    assert h2.record(ac(lon=24.3), T0 + 60 + 25 * 60) == second
    assert [f["id"] for f in h2.flights(T0, T0 + 7200)] == [second, first]
    h2.tick(T0 + 60 + 50 * 60)  # not heard from for 25 minutes
    assert h2.last == {}


def test_prune_keeps_a_year(tmp_path):
    h = history(tmp_path)
    old = h.record(ac(), T0)
    h.record(ac(lon=24.1), T0 + 60)
    new = h.record(ac(lon=24.2), T0 + 200 * 86400)
    assert h.prune(T0 + (KEEP_DAYS + 1) * 86400) == 2
    assert h.flight(old) is None and h.flight(new)["points"] == 1


def test_encoding_and_simplify():
    pts = [(60.12346, 24.98766, T0), (60.2, 24.1, T0 + 31), (59.9, 23.5, T0 + 3600)]
    assert decode(encode(pts)) == [(60.1235, 24.9877, T0), (60.2, 24.1, T0 + 31), (59.9, 23.5, T0 + 3600)]
    line = [(60.0, 24.0 + i * 0.01, T0 + i) for i in range(50)] + [(60.1, 24.5, T0 + 99)]
    assert simplify(line, 50) == [line[0], line[49], line[50]]  # the straight run collapses


def test_watcher_records_in_the_background_and_publishes_only_to_panels(tmp_path):
    h = history(tmp_path)
    sent = []
    panel = {"open": False}
    w = AircraftWatcher(None, lambda: True, sent.append, h, lambda: panel["open"])
    row = {"hex": "461f01", "r": "OH-HMV", "t": "H145", "category": "A7", "flight": "FINNHEMS10",
           "lat": 60.0, "lon": 24.0, "alt_baro": 1500, "gs": 130, "track": 235, "seen_pos": 0}
    w.update([row], T0)
    w.update([{**row, "lat": 60.05}], T0 + 30)
    [entry] = w.snapshot()
    assert entry["flight_id"] and h.flight(entry["flight_id"])["points"] == 2
    assert sent == []


def make_web(tmp_path):
    h = history(tmp_path)
    for i in range(40):
        h.record(ac(lat=60.0 + i * 0.01, lon=24.0 + (i % 7) * 0.01, track=i * 20.0), T0 + i * 30)
    h.tick(T0 + 7200)
    fw = SimpleNamespace(air_history=h)
    return webmod.Web(fw), h


def test_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(webmod.time, "time", lambda: T0 + 3600)
    w, h = make_web(tmp_path)

    async def run():
        req = make_mocked_request("GET", "/api/aircraft/flights?period=24h")
        listing = json.loads((await w.air_flights(req)).body)
        [flight] = listing["flights"]
        req = make_mocked_request("GET", f"/api/aircraft/flights/{flight['id']}",
                                  match_info={"flight_id": str(flight["id"])})
        detail = json.loads((await w.air_flight(req)).body)
        return flight, detail

    flight, detail = asyncio.run(run())
    assert flight["reg"] == "OH-HMV" and flight["points"] == 40 and "track" not in flight
    assert len(detail["track"]) == 40 and detail["track"][0][3] == 305
    # The overview comes out in pieces that make one JSON document.
    data = json.loads("".join(h.overview(T0 - 86400, T0 + 3600)))
    [[fid, icao, start, end, max_alt, km, line]] = data["flights"]
    assert icao == "461f01" and start == T0 and len(decode(line)) >= 2
    assert data["aircraft"]["461f01"]["flights"] == 1 and data["aircraft"]["461f01"]["reg"] == "OH-HMV"
    year = json.loads("".join(h.overview(T0 - 365 * 86400, T0 + 3600)))
    assert len(decode(year["flights"][0][6])) <= len(decode(line))  # coarser for a year
