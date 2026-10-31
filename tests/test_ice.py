import asyncio
import json
from datetime import datetime, timezone

import aiohttp

from ferrywatch import ice
from ferrywatch.ice import IceWatch, chart_tile, ice_state, latest_thickness, parse_chart, parse_timevaluepair
from ferrywatch.store import Store

# The shape of FMI's answer to fmi::observations::seaice::manual::timevaluepair (one member
# per station and parameter; later members point at the first one's times with xlink:href).
FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<wfs:FeatureCollection timeStamp="2026-10-23T02:08:15Z" numberMatched="2" numberReturned="2"
    xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:om="http://www.opengis.net/om/2.0"
    xmlns:omso="http://inspire.ec.europa.eu/schemas/omso/3.0" xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:sam="http://www.opengis.net/sampling/2.0" xmlns:sams="http://www.opengis.net/samplingSpatial/2.0"
    xmlns:wml2="http://www.opengis.net/waterml/2.0" xmlns:target="http://xml.fmi.fi/namespace/om/atmosphericfeatures/1.1">
  <wfs:member>
    <omso:PointTimeSeriesObservation gml:id="obs-obs-1-1">
      <om:phenomenonTime><gml:TimePeriod gml:id="time1-1-1"><gml:beginPosition>2026-02-01T00:00:00Z</gml:beginPosition>
        <gml:endPosition>2026-02-20T00:00:00Z</gml:endPosition></gml:TimePeriod></om:phenomenonTime>
      <om:resultTime><gml:TimeInstant gml:id="time2-1-1"><gml:timePosition>2026-02-20T00:00:00Z</gml:timePosition>
        </gml:TimeInstant></om:resultTime>
      <om:procedure xlink:href="http://xml.fmi.fi/inspire/process/sea_ice_manual"/>
      <om:observedProperty xlink:href="https://opendata.fmi.fi/meta?observableProperty=observation&amp;param=ICE_PT1S_INSTANT&amp;language=eng"/>
      <om:featureOfInterest><sams:SF_SpatialSamplingFeature gml:id="fi-1-1-ICE_PT1S_INSTANT">
        <sam:sampledFeature><target:LocationCollection gml:id="sampled-target-1-1"><target:member>
          <target:Location gml:id="obsloc-fmisid-103933-pos">
            <gml:identifier codeSpace="http://xml.fmi.fi/namespace/stationcode/fmisid">103933</gml:identifier>
            <gml:name codeSpace="http://xml.fmi.fi/namespace/locationcode/name">Kirkkonummi Porkkala</gml:name>
          </target:Location></target:member></target:LocationCollection></sam:sampledFeature>
        <sams:shape><gml:Point gml:id="point-1-1-ICE_PT1S_INSTANT" srsName="http://www.opengis.net/def/crs/EPSG/0/4258">
          <gml:pos>59.95000 24.33333 </gml:pos></gml:Point></sams:shape>
      </sams:SF_SpatialSamplingFeature></om:featureOfInterest>
      <om:result><wml2:MeasurementTimeseries gml:id="obs-obs-1-1-ICE_PT1S_INSTANT">
        <wml2:point><wml2:MeasurementTVP><wml2:time>2026-02-03T11:00:00Z</wml2:time><wml2:value>27.0</wml2:value></wml2:MeasurementTVP></wml2:point>
        <wml2:point><wml2:MeasurementTVP><wml2:time>2026-02-09T10:00:00Z</wml2:time><wml2:value>30.0</wml2:value></wml2:MeasurementTVP></wml2:point>
        <wml2:point><wml2:MeasurementTVP><wml2:time>2026-02-17T10:00:00Z</wml2:time><wml2:value>33.0</wml2:value></wml2:MeasurementTVP></wml2:point>
      </wml2:MeasurementTimeseries></om:result>
    </omso:PointTimeSeriesObservation>
  </wfs:member>
  <wfs:member>
    <omso:PointTimeSeriesObservation gml:id="obs-obs-1-2">
      <om:phenomenonTime xlink:href="#time1-1-1"/>
      <om:resultTime xlink:href="#time2-1-1"/>
      <om:observedProperty xlink:href="https://opendata.fmi.fi/meta?observableProperty=observation&amp;param=SNDICE_PT1M_AVG&amp;language=eng"/>
      <om:result><wml2:MeasurementTimeseries gml:id="obs-obs-1-2-SNDICE_PT1M_AVG">
        <wml2:point><wml2:MeasurementTVP><wml2:time>2026-02-03T11:00:00Z</wml2:time><wml2:value>NaN</wml2:value></wml2:MeasurementTVP></wml2:point>
        <wml2:point><wml2:MeasurementTVP><wml2:time>2026-02-17T10:00:00Z</wml2:time><wml2:value>18.0</wml2:value></wml2:MeasurementTVP></wml2:point>
      </wml2:MeasurementTimeseries></om:result>
    </omso:PointTimeSeriesObservation>
  </wfs:member>
</wfs:FeatureCollection>"""
EMPTY = ('<wfs:FeatureCollection numberMatched="0" numberReturned="0" '
         'xmlns:wfs="http://www.opengis.net/wfs/2.0"></wfs:FeatureCollection>')
FEB17 = datetime(2026, 2, 17, 10, tzinfo=timezone.utc).timestamp()


def test_parse_manual_observations():
    values = parse_timevaluepair(FIXTURE)
    assert [v for _, v in values["ICE_PT1S_INSTANT"]] == [27.0, 30.0, 33.0]
    assert values["SNDICE_PT1M_AVG"] == [(FEB17, 18.0)]  # NaN left out
    assert latest_thickness(values) == {"ts": int(FEB17), "cm": 33.0, "snow_cm": 18.0}
    assert parse_timevaluepair(EMPTY) == {} and latest_thickness({}) is None


def test_parse_chart_answers():
    assert parse_chart({"type": "FeatureCollection", "features": [{"properties": {"value": 87.5}}]}) == 87.5
    assert parse_chart({"features": [{"properties": {"ice_concentration": "40"}}]}) == 40.0
    assert parse_chart({"features": [{"properties": {"value": None}}]}) is None  # land or no chart
    assert parse_chart({"features": []}) is None and parse_chart({"value": 1e20}) is None
    col, row, i, j = chart_tile(59.97, 24.40)
    assert (col, row) == (581, 297) and 0 <= i < 256 and 0 <= j < 256


def test_ice_state():
    now = FEB17 + 86400
    thick = {"ts": FEB17, "cm": 33.0, "snow_cm": 18.0}
    assert ice_state(95.0, None, True, now) == "ice"
    assert ice_state(5.0, thick, True, now) == "open"  # the chart speaks for the route
    assert ice_state(None, thick, True, now) == "ice"
    assert ice_state(None, thick, True, now + 20 * 86400) is None  # an old report: can't tell
    assert ice_state(None, None, True, now) == "open"  # no reports in a month: no ice
    assert ice_state(None, None, False, now) is None


def test_daily_check_stores_a_row_and_logs_failures_once(tmp_path, caplog, monkeypatch):
    store = Store(tmp_path / "t.db")
    calls = []

    async def fetch(url, params):
        calls.append(url)
        if "copernicus" in url:
            raise aiohttp.ClientConnectionError("blocked")
        assert params["fmisid"] == 103933 and params["storedquery_id"].startswith("fmi::observations::seaice")
        return FIXTURE

    watch = IceWatch(None, store, fetch)
    now = FEB17 + 86400
    monkeypatch.setattr(ice, "is_winter", lambda ts: True)
    row = asyncio.run(watch.current(now))
    assert row["state"] == "ice" and row["thick_cm"] == 33.0 and row["conc"] is None and row["snow_cm"] == 18.0
    assert asyncio.run(watch.current(now + 60)) == row and len(calls) == 2  # once a day
    asyncio.run(watch.check(now + 120))
    assert sum("ice chart" in r.message for r in caplog.records) == 1
    monkeypatch.setattr(ice, "is_winter", lambda ts: False)
    assert asyncio.run(watch.current(now)) is None  # summer: nothing is read


def test_chart_json_text_is_parsed(tmp_path):
    async def fetch(url, params):
        return json.dumps({"features": [{"properties": {"value": 0.0}}]}) if "copernicus" in url else EMPTY

    row = asyncio.run(IceWatch(None, Store(tmp_path / "t.db"), fetch).check(FEB17))
    assert row["state"] == "open" and row["conc"] == 0.0 and row["thick_cm"] is None
