import sqlite3
import time

from ferrywatch.portcalls import PortTimes, VisitLog, parse_calls, place

H = 3600


def _db():
    return sqlite3.connect(":memory:")


def test_parse_calls_reads_times_and_ports():
    data = {"portCalls": [{
        "portCallId": 1, "mmsi": 230184000, "portToVisit": "FIHEL", "prevPort": "SESTO", "nextPort": "FIMHQ",
        "portAreaDetails": [{"berthName": "Olympia", "eta": "2026-11-09T08:30:00.000+00:00",
                             "etd": "2026-11-09T15:00:00Z", "ata": None, "atd": None}],
    }, {"portCallId": 2, "mmsi": None}]}
    calls = parse_calls(data)
    assert len(calls) == 1
    c = calls[0]
    assert (c["port"], c["prev"], c["next"], c["berth"]) == ("Helsinki", "Stockholm", "Mariehamn", "Olympia")
    assert c["etd"] - c["eta"] == 6.5 * H
    assert place("XXABC") == "XXABC"


def test_visits_from_positions_need_a_real_stop():
    log = VisitLog(_db())
    t = 1_000_000
    for i in range(10):  # 9 minutes stopped at Helsinki
        log.see(1, t + i * 60, 60.160, 24.955, 0.2)
    log.see(1, t + 700, 60.15, 24.96, 12.0)
    log.see(1, t + 2 * H, 60.09, 19.93, 0.1)  # a 2-minute stop in Mariehamn doesn't count
    log.see(1, t + 2 * H + 120, 60.09, 19.93, 0.1)
    log.see(1, t + 2 * H + 150, 60.09, 19.93, 8.0)
    assert log.visits(1) == [("Helsinki", t, t + 700)]


def _times(tmp_path, visits):
    db = _db()
    pt = PortTimes(None, db, tmp_path / "pc.json.gz")
    db.executemany("INSERT INTO port_visits (mmsi, port, arrive_ts, leave_ts) VALUES (?, ?, ?, ?)", visits)
    db.commit()
    return pt


def test_at_sea_usual_crossing_and_next_port(tmp_path):
    now = 10_000_000
    visits = []
    for k in range(5):  # Helsinki -> Tallinn 2 h, an hour in Tallinn, back
        b = now - (k + 1) * 6 * H
        visits += [(7, "Helsinki", b, b + H), (7, "Tallinn", b + 3 * H, b + 4 * H)]
    visits.sort(key=lambda v: v[2])
    visits.append((7, "Helsinki", now - 2 * H, now - H))  # left an hour ago
    pt = _times(tmp_path, visits)
    rows = pt.summary(7, {"position": {"ts": now}}, now=now)["rows"]
    assert [r["port"] for r in rows] == ["Helsinki", "Tallinn", "Helsinki"]
    assert rows[0]["leave"] == {"ts": now - H, "actual": True, "src": "ais"}
    assert rows[1]["arrive"]["ts"] == now + H and rows[1]["arrive"]["src"] == "usual"
    assert rows[1]["leave"]["ts"] == now + 2 * H


def test_portnet_times_win_and_ais_eta_for_foreign_port(tmp_path):
    now = 10_000_000
    pt = _times(tmp_path, [(5, "Helsinki", now - 6 * H, now - H)])
    pt.calls = {c["id"]: c for c in [
        {"id": 1, "mmsi": 5, "port": "Helsinki", "prev": "Stockholm", "next": "Mariehamn", "berth": "Olympia",
         "eta": now - 6 * H, "ata": now - 6 * H, "etd": now - H, "atd": now - H + 60},
        {"id": 2, "mmsi": 5, "port": "Mariehamn", "prev": "Helsinki", "next": "Stockholm", "berth": None,
         "eta": now + 10 * H, "etd": now + 10.25 * H, "ata": None, "atd": None},
    ]}
    ship = {"destination": "SESTO", "eta_ais": now + 16 * H}
    rows = pt.summary(5, ship, now=now)["rows"]
    assert [r["port"] for r in rows] == ["Helsinki", "Mariehamn", "Stockholm"]
    assert rows[0]["berth"] == "Olympia"
    assert rows[1]["arrive"] == {"ts": now + 10 * H, "actual": False, "src": "portnet"}
    assert rows[2]["arrive"] == {"ts": now + 16 * H, "actual": False, "src": "ais_eta"}


def test_in_port_leaves_by_portnet(tmp_path):
    now = 10_000_000
    pt = _times(tmp_path, [(5, "Helsinki", now - 2 * H, None)])
    pt.calls = {1: {"id": 1, "mmsi": 5, "port": "Helsinki", "prev": None, "next": "Tallinn", "berth": "Katajanokka",
                    "eta": now - 2 * H, "ata": now - 2 * H, "etd": now + 3 * H, "atd": None}}
    rows = pt.summary(5, {}, now=now)["rows"]
    assert rows[0]["leave"] == {"ts": now + 3 * H, "actual": False, "src": "portnet"}
    assert rows[1] == {"port": "Tallinn", "arrive": None, "leave": None}


def test_other_ship_only_portnet(tmp_path):
    now = 10_000_000
    pt = _times(tmp_path, [])
    pt.calls = {1: {"id": 1, "mmsi": 9, "port": "Helsinki", "prev": "Hamburg", "next": "Stockholm", "berth": "Vuosaari",
                    "eta": now + 2 * H, "etd": now + 14 * H, "ata": None, "atd": None}}
    rows = pt.summary(9, {}, now=now)["rows"]
    assert rows[0]["port"] == "Helsinki" and rows[0]["arrive"]["ts"] == now + 2 * H
    assert rows[1]["port"] == "Stockholm"


def test_save_and_load(tmp_path):
    db = _db()
    pt = PortTimes(None, db, tmp_path / "pc.json.gz")
    pt.calls = {1: {"id": 1, "mmsi": 9, "port": "Helsinki", "eta": time.time()}}
    pt.fetched = 5.0
    pt._save()
    again = PortTimes(None, db, tmp_path / "pc.json.gz")
    assert again.calls[1]["mmsi"] == 9 and again.fetched == 5.0
