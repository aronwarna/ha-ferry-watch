"""Blue-green algae: SYKE's satellite days and tap values, the Open311 reports kept at sea."""

import asyncio
import json

from ferrywatch import algae, simalgae
from ferrywatch.overlays import Overlays, parse_instant

CAPS = ("<WMS_Capabilities><Layer><Layer><Name>EO_OTHER</Name><Dimension name=\"time\">2020-01-01T00:00:00.000Z"
        "</Dimension></Layer><Layer queryable=\"1\"><Name>eo:EO_MR_OLCI_ALGAE</Name>"
        "<Dimension name=\"time\" default=\"2026-10-01T00:00:00Z\" units=\"ISO8601\">"
        "2025-07-01T00:00:00.000Z,2025-08-15T00:00:00.000Z,2026-05-20T00:00:00.000Z,2026-06-02T00:00:00.000Z,"
        "2026-07-21T00:00:00.000Z,2026-09-30T00:00:00.000Z,2026-10-01T00:00:00.000Z</Dimension></Layer></Layer>"
        "</WMS_Capabilities>")


def day(text):
    return parse_instant(text + "T00:00:00Z")


def test_days_of_the_right_layer():
    days = algae.days_from_capabilities(CAPS)
    assert days[0] == day("2025-07-01") and days[-1] == day("2026-10-01") and len(days) == 7


def test_out_of_season_shows_last_summer():
    days = algae.days_from_capabilities(CAPS)
    now = parse_instant("2026-11-09T12:00:00Z")
    assert algae.pick_days(days, now) == [day("2026-06-02"), day("2026-07-21"), day("2026-09-30")]
    # In spring, last year's season.
    spring = algae.pick_days(days, parse_instant("2026-03-01T00:00:00Z"))
    assert spring == [day("2025-07-01"), day("2025-08-15")]


def test_in_season_the_last_weeks():
    days = algae.days_from_capabilities(CAPS)
    now = parse_instant("2026-07-25T12:00:00Z")
    assert algae.pick_days(days, now) == [day("2026-07-21")]  # 2 June is over 45 days back
    start, end = algae.season_window(now)
    assert end == day("2026-07-25") and start == end - (algae.IN_SEASON_DAYS - 1) * 86400


def test_feature_index():
    assert algae.feature_index({"features": [{"properties": {"algae_index": 3}}]}) == 3
    assert algae.feature_index({"algae_index": 0}) is None
    assert algae.feature_index({"features": []}) is None


def test_satellite_times_and_value(tmp_path):
    ov = Overlays(tmp_path, None)

    async def get(url, params):
        if params["REQUEST"] == "GetCapabilities":
            return CAPS
        return json.dumps({"features": [{"properties": {"algae_index": 4}}]})
    algae.register(ov, get, simalgae.fake_reports)
    o = ov.registry[algae.ID]
    assert o.sea and o.value and o.kind == "tiles"
    times = asyncio.run(o.times(None, parse_instant("2026-11-09T12:00:00Z")))
    assert times[-1] == day("2026-09-30")
    assert "TIME=2026-07-21" in o.tile_url(day("2026-07-21"), 8, 145, 74)
    got = asyncio.run(o.value(59.9, 24.4, day("2026-07-21")))
    assert got == {"index": 4, "date": "2026-07-21"}


def test_parse_report():
    r = {"lat": 60.15614, "long": 24.78386, "requested_datetime": "2025-07-20T21:54:00.0000000",
         "description": "This observation was transferred from #Lakewiki web service. It belongs to the observation "
                        "site: www.jarviwiki.fi/wiki?curid=289941. Tags: #_jwObservation, #_jwSiteID_289941, "
                        "#_jwMaintainer_expert, #_jwMonitoring_Valtakunnallinen-leväseuranta, #_jwCampaign_RotaryBalticSea "
                        "Ecology_algaebloom_singlevaluelist_201808151546174:2,Ecology_additionalinfo:Rannassa lauttoja"}
    p = algae.parse_report(r)
    assert p["level"] == 2 and p["kind"] == 2 and p["site"] == 289941 and p["note"] == "Rannassa lauttoja"
    assert p["ts"] == parse_instant("2025-07-20T18:54:00Z")  # Finnish summer time
    r["description"] = "#_VesiFiObservation Ecology_algaebloom_singlevaluelist_201808151546174:0,Ecology_additionalinfo:"
    p = algae.parse_report(r)
    assert p["level"] == 0 and p["kind"] == 0 and p["note"] == ""
    assert algae.parse_report({"lat": 1, "long": 2, "description": "nothing"}) is None


def test_at_sea_keeps_the_shore_not_lakes():
    assert algae.at_sea(59.95, 24.5)  # open Gulf of Finland
    assert algae.at_sea(60.1561, 24.7839)  # a beach in Espoo
    assert not algae.at_sea(61.498, 23.761)  # Tampere, by a lake
    assert not algae.at_sea(62.241, 25.744)  # Jyväskylä


def test_reports_run_and_tap(tmp_path):
    ov = Overlays(tmp_path, None)
    rep = algae.register(ov, simalgae.fake_get, simalgae.fake_reports)
    now = parse_instant("2026-11-09T12:00:00Z")
    o = ov.registry[algae.REPORTS_ID]
    times = asyncio.run(ov.times(o, now))
    assert times[0] == day("2026-06-01") and times[-1] == day("2026-09-30")
    assert o.max_age == 7 * 86400  # out of season, last season's reports stay a week
    data = asyncio.run(ov.points(algae.REPORTS_ID, day("2026-08-01"), now))
    assert data["fields"] == ["level", "kind", "age"] and data["points"]
    assert all(p[2] is not None for p in data["points"])  # sparse: only that day's reports
    assert all(p[0] < 61 for p in data["points"])  # the lakes' reports were left out
    lat, lon = data["points"][0][:2]
    got = asyncio.run(rep.value(lat, lon, day("2026-08-01")))
    assert got["reports"][0]["level_text"] in algae.LEVELS.values()
    assert asyncio.run(rep.value(10.0, 10.0, day("2026-08-01"))) is None


def test_big_weeks_are_split():
    calls = []

    async def get(url, params):
        calls.append((params["start_date"], params["end_date"]))
        if len(calls) == 1:
            return [{"service_request_id": i} for i in range(algae.CAP)]
        return [{"service_request_id": len(calls)}]
    rows = asyncio.run(algae.fetch_reports(get, day("2025-07-01"), day("2025-07-08")))
    assert len(calls) == 3 and len(rows) == 2
