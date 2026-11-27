"""Nautical warnings, sea weather warnings, bridges and locks, and the speed limits on a route."""
import asyncio
import time

from ferrywatch import notices, simnotices


def test_nautical_warnings_any_geometry_and_only_those_in_force():
    data = simnotices.nautical()
    data["features"].append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [24.4, 59.9]},
                             "properties": {"id": 9, "typeEn": "OLD", "validityEndTime": "2020-01-01T00:00:00Z"}})
    data["features"].append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [5.0, 53.0]},
                             "properties": {"id": 10, "typeEn": "NORTH SEA"}})
    data["features"].append({"type": "Feature", "geometry": None, "properties": {"id": 11}})
    got = notices.parse_nautical(data)
    assert [w["id"] for w in got] == [1001, 1002, 1003]
    assert [w["shapes"][0]["kind"] for w in got] == ["area", "point", "line"]
    assert got[0]["place"] == "SOUTH OF PORKKALA" and "FIRING" in got[0]["text"]
    assert got[1]["end"] is None and got[0]["end"] > time.time()
    assert 59.86 < got[0]["at"][0] < 59.93  # [lat, lon]


def test_field_names_are_read_loosely():
    data = {"features": [{"geometry": {"type": "Point", "coordinates": [24.4, 59.9]},
                          "properties": {"ID": 5, "type_fi": "PAIKALLISVAROITUS", "contents_fi": "Viitta poissa.",
                                         "validity_start_time": 1794000000000}}]}
    (w,) = notices.parse_nautical(data)
    assert w["type"] == "PAIKALLISVAROITUS" and w["text"] == "Viitta poissa." and w["start"] == 1794000000


def test_sea_weather_warnings_from_cap():
    got = notices.parse_cap(simnotices.cap())
    assert [w["code"] for w in got] == ["seaWaveHeight", "seaWind"]  # orange first; the wildfire one is left out
    wind = got[1]
    assert wind["wind_ms"] == 15 and wind["wind_dir"] == 225 and wind["color"] == "yellow"
    assert wind["event"] == "Wind warning for sea areas"  # the English text, not the Finnish
    assert wind["areas"][0]["name"] == "Western Gulf of Finland" and len(wind["areas"][0]["rings"][0]) == 6
    here = notices.warnings_at(got, 59.98, 24.4)
    assert [w["code"] for w in here] == ["seaWind"] and here[0]["area"] == "Western Gulf of Finland"
    assert notices.warnings_at(got, 61.5, 21.0) == []
    assert notices.parse_cap(simnotices.cap(), now=time.time() + 30 * 86400) == []  # all expired


def test_bridges_locks_and_canals():
    bridge, = notices.parse_structures(simnotices.wfs(notices.STRUCTURES["bridge"]), "bridge")
    assert bridge["name"] == "Simsalmen läppäsilta" and bridge["height_m"] == 3.2 and bridge["width_m"] == 9.0
    assert abs(bridge["at"][0] - 60.045) < 1e-4 and abs(bridge["at"][1] - 24.415) < 1e-4
    assert "OBJECTID" not in bridge["props"] and bridge["props"]["tyyppi"] == "Läppäsilta"
    lock, = notices.parse_structures(simnotices.wfs(notices.STRUCTURES["lock"]), "lock")
    assert lock["length_m"] == 35.0 and lock["depth_m"] == 2.4
    canal, = notices.parse_structures(simnotices.wfs(notices.STRUCTURES["canal"]), "canal")
    assert canal["shapes"][0]["kind"] == "line" and len(canal["shapes"][0]["points"]) == 3


def test_speed_limits_on_a_route(tmp_path):
    areas = notices.parse_limits(simnotices.wfs(notices.RESTRICT_TYPE))
    assert sorted(a["kmh"] for a in areas) == [10, 30]  # the area that isn't a speed limit is left out
    # West to east along 60.0 N, through the 10 km/h box between 24.47 and 24.55 E.
    route = [[60.0, 24.40], [60.0, 24.60]]
    got = notices.limits_on_route(route, areas, speed_kn=20)
    (s,) = got["stretches"]
    assert s["kmh"] == 10 and s["kn"] == 5.4
    lon_m = 111_320 * 0.5  # a degree of longitude at 60 N
    assert abs(s["from_m"] - 0.07 * lon_m) < 80 and abs(s["to_m"] - 0.15 * lon_m) < 80
    nm = (s["to_m"] - s["from_m"]) / 1852
    assert abs(got["extra_min"] - (nm / 5.4 - nm / 20) * 60) < 0.2
    assert notices.limits_on_route(route, areas, speed_kn=4)["extra_min"] == 0  # slower than the limit anyway
    assert notices.limits_on_route([[59.5, 24.0], [59.5, 24.2]], areas)["stretches"] == []

    n = notices.Notices(tmp_path, None, simnotices.FAKE)
    assert asyncio.run(n.route_limits(route, 20))["stretches"][0]["kmh"] == 10
    items = asyncio.run(n.structures())
    assert {i["kind"] for i in items} == {"bridge", "lock", "canal"}
    assert (tmp_path / "structures.json.gz").is_file()
    n2 = notices.Notices(tmp_path, None, {"wfs": lambda *a: 1 / 0})  # read from disk, the service not asked
    assert len(asyncio.run(n2.structures())) == 3


def test_nautical_from_traficom_layers_with_unknown_field_names():
    from ferrywatch import notices as n
    x, y = n._merc(59.95, 24.45) if hasattr(n, "_merc") else (None, None)
    data = {"mercator": True, "features": [{
        "type": "Feature", "id": "navigational_warnings_p.7",
        "geometry": {"type": "Point", "coordinates": [x, y]},
        "properties": {"OBJECTID": 7, "warning_number": 123, "type_fi": "Paikallisvaroitus", "type_en": "Local warning",
                       "location_fi": "Porkkala", "content_fi": "Viitta poissa", "content_en": "Spar buoy missing",
                       "validity_start": "2026-11-01T08:00:00Z", "validity_end": None,
                       "publishing_time": "2026-11-01T07:30:00Z", "source": "VTS"},
    }]}
    w = n.parse_nautical(data, now=1794470000)
    assert len(w) == 1
    w = w[0]
    assert w["type"] == "Local warning" and w["text"] == "Spar buoy missing" and w["number"] == 123
    assert w["place"] == "Porkkala" and w["start"] and w["published"] and w["end"] is None
    assert abs(w["at"][0] - 59.95) < 1e-3 and abs(w["at"][1] - 24.45) < 1e-3
    assert w["props"].get("source") == "VTS" and "OBJECTID" not in w["props"]
