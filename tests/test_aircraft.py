from ferrywatch.aircraft import AircraftWatcher, interesting, operator


def row(**kw):
    base = {"hex": "abc123", "r": "OH-HVK", "t": "AW19", "category": "A7", "flight": "FGU11 ",
            "lat": 60.0, "lon": 24.0, "alt_baro": 1200, "gs": 120, "track": 90, "seen_pos": 1}
    return {**base, **kw}


def test_picks_finnish_rescue_aircraft():
    assert interesting(row())  # Border Guard helicopter
    assert interesting(row(r="OH-HMV", t="H145", category=None))  # helicopter by type
    assert interesting(row(r="OH-MVL", t="D228", category="A1"))  # Border Guard plane
    assert not interesting(row(r="OH-LWA", t="A321", category="A3"))  # Finnair
    assert not interesting(row(r="SE-JRE", t="H145"))  # not Finnish
    assert operator("OH-HMV", "FINNHEMS10") == "FinnHEMS"
    assert operator("OH-HVK", "FGU11") == "Border Guard"


def test_trail_and_forgetting():
    sent = []
    w = AircraftWatcher(None, lambda: True, sent.append)
    w.update([row(), row(r="OH-LWA", t="A321", category="A3", hex="x")], now=1000)
    w.update([row(lat=60.1)], now=1010)
    [ac] = w.snapshot()
    assert ac["operator"] == "Border Guard" and ac["callsign"] == "FGU11" and ac["helicopter"]
    assert ac["trail"] == [[60.0, 24.0], [60.1, 24.0]]
    w.update([], now=1200)  # not heard from for over two minutes
    assert w.snapshot() == []


def test_type_names_and_details():
    w = AircraftWatcher(None, lambda: True, lambda _: None)
    w.update([row(t="A169", desc=None, ownOp="SUOMEN RAJAVARTIOLAITOS", year="2021", baro_rate=-700,
                  emergency="none", squawk="7000"),
              row(hex="def456", r="OH-HMV", t="EC45", flight="FINNHEMS10", desc="AIRBUS HELICOPTERS H-145",
                  emergency="general")], now=1000)
    bg, hems = sorted(w.snapshot(), key=lambda a: a["reg"])[::-1]
    assert bg["type_name"] == "Leonardo AW169" and bg["owner"] == "Suomen Rajavartiolaitos"
    assert bg["climb_fpm"] == -700 and bg["emergency"] is None
    assert hems["type_name"] == "Airbus Helicopters H-145" and hems["emergency"] == "general"
