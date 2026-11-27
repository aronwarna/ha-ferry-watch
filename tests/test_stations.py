import asyncio
import time

import aiohttp

from ferrywatch import stations as stations_mod
from ferrywatch.simsea import fake_multipoint
from ferrywatch.stations import Stations, build_stations, parse_multipoint

# The shape of an FMI multipointcoverage answer (fmi::observations::weather::multipointcoverage
# with bbox and parameters=windspeedms,windgust,winddirection,temperature, timestep=10):
# a location list, the station points, then "lat lon unixtime" rows and a value row for each.
FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<wfs:FeatureCollection timeStamp="2026-10-19T08:41:12Z" numberMatched="1" numberReturned="1"
    xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:om="http://www.opengis.net/om/2.0"
    xmlns:omso="http://inspire.ec.europa.eu/schemas/omso/3.0" xmlns:sam="http://www.opengis.net/sampling/2.0"
    xmlns:sams="http://www.opengis.net/samplingSpatial/2.0" xmlns:gmlcov="http://www.opengis.net/gmlcov/1.0"
    xmlns:swe="http://www.opengis.net/swe/2.0" xmlns:target="http://xml.fmi.fi/namespace/om/atmosphericfeatures/1.1">
  <wfs:member>
    <omso:GridSeriesObservation gml:id="obs-obs-1-1">
      <om:phenomenonTime><gml:TimePeriod gml:id="time1-1-1"><gml:beginPosition>2026-10-19T08:00:00Z</gml:beginPosition>
        <gml:endPosition>2026-10-19T08:10:00Z</gml:endPosition></gml:TimePeriod></om:phenomenonTime>
      <om:featureOfInterest>
        <sams:SF_SpatialSamplingFeature gml:id="sampling-feature-1-1-fmisid">
          <sam:sampledFeature>
            <target:LocationCollection gml:id="sampled-target-1-1">
              <target:member>
                <target:Location gml:id="obsloc-fmisid-100997-pos">
                  <gml:identifier codeSpace="http://xml.fmi.fi/namespace/stationcode/fmisid">100997</gml:identifier>
                  <gml:name codeSpace="http://xml.fmi.fi/namespace/locationcode/name">Kirkkonummi Mäkiluoto</gml:name>
                  <gml:name codeSpace="http://xml.fmi.fi/namespace/locationcode/geoid">-16000096</gml:name>
                  <gml:name codeSpace="http://xml.fmi.fi/namespace/locationcode/wmo">2794</gml:name>
                  <target:representativePoint xlink:href="#point-100997"/>
                  <target:region codeSpace="http://xml.fmi.fi/namespace/location/region">Kirkkonummi</target:region>
                </target:Location>
              </target:member>
              <target:member>
                <target:Location gml:id="obsloc-fmisid-101339-pos">
                  <gml:identifier codeSpace="http://xml.fmi.fi/namespace/stationcode/fmisid">101339</gml:identifier>
                  <gml:name codeSpace="http://xml.fmi.fi/namespace/locationcode/name">Jyväskylä lentoasema</gml:name>
                  <target:representativePoint xlink:href="#point-101339"/>
                </target:Location>
              </target:member>
            </target:LocationCollection>
          </sam:sampledFeature>
          <sams:shape>
            <gml:MultiPoint gml:id="mp-1-1-fmisid">
              <gml:pointMember>
                <gml:Point gml:id="point-100997" srsName="http://www.opengis.net/def/crs/EPSG/0/4258" srsDimension="2">
                  <gml:name>Kirkkonummi Mäkiluoto</gml:name>
                  <gml:pos>59.91982 24.35023 </gml:pos>
                </gml:Point>
              </gml:pointMember>
              <gml:pointMember>
                <gml:Point gml:id="point-101339" srsName="http://www.opengis.net/def/crs/EPSG/0/4258" srsDimension="2">
                  <gml:name>Jyväskylä lentoasema</gml:name>
                  <gml:pos>62.39758 25.67087 </gml:pos>
                </gml:Point>
              </gml:pointMember>
            </gml:MultiPoint>
          </sams:shape>
        </sams:SF_SpatialSamplingFeature>
      </om:featureOfInterest>
      <om:result>
        <gmlcov:MultiPointCoverage gml:id="mpcv-1-1-fmisid">
          <gml:domainSet>
            <gmlcov:SimpleMultiPoint gml:id="mp-1-1-fmisid" srsName="http://xml.fmi.fi/gml/crs/compoundCRS.php?crs=4258&amp;time=unixtime" srsDimension="3">
              <gmlcov:positions>
                59.91982 24.35023  1792396800
                59.91982 24.35023  1792397400
                62.39758 25.67087  1792396800
                62.39758 25.67087  1792397400
              </gmlcov:positions>
            </gmlcov:SimpleMultiPoint>
          </gml:domainSet>
          <gml:rangeSet>
            <gml:DataBlock>
              <gml:rangeParameters/>
              <gml:doubleOrNilReasonTupleList>
                8.1 11.0 223.0 9.6
                9.4 NaN 231.0 9.4
                2.1 4.0 180.0 6.0
                2.5 4.2 185.0 6.1
              </gml:doubleOrNilReasonTupleList>
            </gml:DataBlock>
          </gml:rangeSet>
          <gml:coverageFunction><gml:CoverageMappingRule><gml:ruleDefinition>Linear</gml:ruleDefinition></gml:CoverageMappingRule></gml:coverageFunction>
          <gmlcov:rangeType>
            <swe:DataRecord>
              <swe:field name="windspeedms" xlink:href="https://opendata.fmi.fi/meta?observableProperty=observation&amp;param=windspeedms&amp;language=eng"/>
              <swe:field name="windgust" xlink:href="https://opendata.fmi.fi/meta?observableProperty=observation&amp;param=windgust&amp;language=eng"/>
              <swe:field name="winddirection" xlink:href="https://opendata.fmi.fi/meta?observableProperty=observation&amp;param=winddirection&amp;language=eng"/>
              <swe:field name="temperature" xlink:href="https://opendata.fmi.fi/meta?observableProperty=observation&amp;param=temperature&amp;language=eng"/>
            </swe:DataRecord>
          </gmlcov:rangeType>
        </gmlcov:MultiPointCoverage>
      </om:result>
    </omso:GridSeriesObservation>
  </wfs:member>
</wfs:FeatureCollection>
"""


def test_parse_multipoint_fixture():
    sites, series, fields = parse_multipoint(FIXTURE)
    assert fields == ["windspeedms", "windgust", "winddirection", "temperature"]
    assert sites[100997] == ("Kirkkonummi Mäkiluoto", 59.91982, 24.35023)
    assert series[100997] == [(1792396800, [8.1, 11.0, 223.0, 9.6]), (1792397400, [9.4, None, 231.0, 9.4])]
    assert len(series[101339]) == 2


def test_inland_stations_left_out_and_latest_values_kept():
    stations = build_stations(parse_multipoint(FIXTURE))
    assert list(stations) == [100997]  # Jyväskylä is inland
    st = stations[100997]
    # The latest wind; the gust is missing in the last row, so the one before is kept.
    assert (st["ws"], st["wg"], st["wd"], st["t"], st["ts"]) == (9.4, 11.0, 231.0, 9.4, 1792397400)


def test_sea_values_join_a_nearby_station_or_stand_alone():
    now = 1792397400
    params = lambda q, p: {"storedquery_id": q, "parameters": p, "timestep": 60,  # noqa: E731
                           "starttime": "2026-10-19T05:00:00Z"}
    weather = parse_multipoint(fake_multipoint({**params("fmi::observations::weather::multipointcoverage",
                                                         stations_mod.WEATHER_PARAMS), "timestep": 10}, now))
    wave = parse_multipoint(fake_multipoint(params(stations_mod.WAVE_QUERY, stations_mod.WAVE_PARAMS), now))
    sea = parse_multipoint(fake_multipoint(params(stations_mod.SEA_QUERY, stations_mod.SEA_PARAMS), now))
    stations = build_stations(weather, wave, sea)
    assert 101339 not in stations
    kaivopuisto = stations[100971]  # the mareograph is 30 m away: sea temperature on the weather station
    assert kaivopuisto["sea"] > 0 and kaivopuisto["sea_from"] == "Helsinki Kaivopuisto"
    assert stations[134221]["wave"] > 0 and "ws" not in stations[134221]  # the buoy on its own
    assert 132310 not in stations


def test_cached_refresh_history_and_error_once(caplog):
    calls = []

    async def fetch(params):
        calls.append(params)
        if fail:
            raise aiohttp.ClientError("blocked")
        return fake_multipoint(params)

    async def run():
        nonlocal fail
        s = Stations(None, fetch)
        listed = await s.latest()
        assert len(listed) > 15 and len(calls) == 3
        assert calls[0]["bbox"] == "19,59.3,30,66" and calls[0]["timestep"] == 10
        await s.latest()
        assert len(calls) == 3  # kept for 10 minutes
        points = await s.history(100997)
        assert 24 <= len(points) <= 32 and points == sorted(points)
        assert points[-1][0] >= time.time() - 1200
        await s.history(100997)
        assert len(calls) == 4  # the history is asked for once an hour
        assert await s.history(1) is None
        assert s.memory_bytes() < 100_000
        fail = True
        s.fetched = 0
        await s.latest()
        s.fetched = 0
        await s.latest()
        assert s.error == "blocked" and len(s.stations) > 15  # the old values stay

    fail = False
    with caplog.at_level("WARNING"):
        asyncio.run(run())
    assert sum("weather stations" in r.message for r in caplog.records) == 1
