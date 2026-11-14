import asyncio
import time
from datetime import date, datetime, timezone

import aiohttp

from ferrywatch.geo import sun_times
from ferrywatch.notify import conditions_line
from ferrywatch.weather import SeaWeather, alert_parts, parse_simple, visibility_text, weather_word

# The shape of an FMI "simple" answer (fmi::observations::weather::simple, Mäkiluoto).
FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<wfs:FeatureCollection timeStamp="2026-10-19T08:41:12Z" numberMatched="6" numberReturned="6"
    xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:om="http://www.opengis.net/om/2.0"
    xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:BsWfs="http://xml.fmi.fi/schema/wfs/2.0">
  <wfs:member>
    <BsWfs:BsWfsElement gml:id="BsWfsElement.1.1.1">
      <BsWfs:Location><gml:Point gml:id="BsWfsElementP.1.1.1" srsDimension="2"
        srsName="http://www.opengis.net/def/crs/EPSG/0/4258"><gml:pos>59.91982 24.35023 </gml:pos></gml:Point></BsWfs:Location>
      <BsWfs:Time>2026-10-19T08:20:00Z</BsWfs:Time>
      <BsWfs:ParameterName>ws_10min</BsWfs:ParameterName>
      <BsWfs:ParameterValue>8.1</BsWfs:ParameterValue>
    </BsWfs:BsWfsElement>
  </wfs:member>
  <wfs:member>
    <BsWfs:BsWfsElement gml:id="BsWfsElement.1.1.2">
      <BsWfs:Time>2026-10-19T08:20:00Z</BsWfs:Time>
      <BsWfs:ParameterName>wd_10min</BsWfs:ParameterName>
      <BsWfs:ParameterValue>223.0</BsWfs:ParameterValue>
    </BsWfs:BsWfsElement>
  </wfs:member>
  <wfs:member>
    <BsWfs:BsWfsElement gml:id="BsWfsElement.1.2.1">
      <BsWfs:Time>2026-10-19T08:30:00Z</BsWfs:Time>
      <BsWfs:ParameterName>ws_10min</BsWfs:ParameterName>
      <BsWfs:ParameterValue>9.4</BsWfs:ParameterValue>
    </BsWfs:BsWfsElement>
  </wfs:member>
  <wfs:member>
    <BsWfs:BsWfsElement gml:id="BsWfsElement.1.2.2">
      <BsWfs:Time>2026-10-19T08:30:00Z</BsWfs:Time>
      <BsWfs:ParameterName>wd_10min</BsWfs:ParameterName>
      <BsWfs:ParameterValue>NaN</BsWfs:ParameterValue>
    </BsWfs:BsWfsElement>
  </wfs:member>
  <wfs:member>
    <BsWfs:BsWfsElement gml:id="BsWfsElement.1.2.3">
      <BsWfs:Time>2026-10-19T08:30:00Z</BsWfs:Time>
      <BsWfs:ParameterName>wg_10min</BsWfs:ParameterName>
      <BsWfs:ParameterValue>13.2</BsWfs:ParameterValue>
    </BsWfs:BsWfsElement>
  </wfs:member>
  <wfs:member>
    <BsWfs:BsWfsElement gml:id="BsWfsElement.1.2.4">
      <BsWfs:Time>2026-10-19T08:30:00Z</BsWfs:Time>
      <BsWfs:ParameterName>t2m</BsWfs:ParameterName>
      <BsWfs:ParameterValue>9.6</BsWfs:ParameterValue>
    </BsWfs:BsWfsElement>
  </wfs:member>
</wfs:FeatureCollection>"""
T820 = datetime(2026, 10, 19, 8, 20, tzinfo=timezone.utc).timestamp()
T830 = T820 + 600


def element(name, value, ts):
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    return (f"<wfs:member><BsWfs:BsWfsElement><BsWfs:Time>{when}</BsWfs:Time><BsWfs:ParameterName>{name}"
            f"</BsWfs:ParameterName><BsWfs:ParameterValue>{value}</BsWfs:ParameterValue></BsWfs:BsWfsElement></wfs:member>")


def answer(*elements):
    return ('<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" '
            'xmlns:BsWfs="http://xml.fmi.fi/schema/wfs/2.0">' + "".join(elements) + "</wfs:FeatureCollection>")


def test_parse_simple_keeps_latest_real_value():
    assert parse_simple(FIXTURE) == {
        "ws_10min": (9.4, T830), "wd_10min": (223.0, T820),  # the newer direction is NaN
        "wg_10min": (13.2, T830), "t2m": (9.6, T830),
    }
    assert parse_simple(answer()) == {}


def fake_fmi(now, fail=()):
    async def fetch(params):
        station = params["fmisid"]
        assert params["starttime"].endswith("Z") and params["parameters"]
        if station in fail:
            raise aiohttp.ClientConnectionError("blocked")
        if station == 100997:
            return answer(element("ws_10min", 8.6, now - 600), element("wg_10min", 12.4, now - 600),
                          element("wd_10min", 226, now - 600), element("t2m", 9.1, now - 600))
        if station == 100996:
            return answer(element("vis", 18500, now - 600), element("wawa", 10, now - 600))
        if station == 132310:
            return answer(element("WATLEV", 163, now - 1800), element("TW_PT1H_AVG", 12.7, now - 1800))
        return answer(element("WaveHs", "NaN", now - 1800))  # the wave buoy is out for the winter
    return fetch


def test_sea_weather_conditions():
    now = float(int(time.time()))
    weather = SeaWeather(None, fake_fmi(now))
    cond = asyncio.run(weather.latest())
    assert cond["wind"] == {"ms": 8.6, "gust": 12.4, "dir": 226.0, "ts": now - 600, "where": "Kirkkonummi Mäkiluoto"}
    assert cond["air"]["c"] == 9.1
    assert cond["visibility"]["km"] == 18.5 and cond["visibility"]["weather"] == "mist"
    assert cond["sea_level"]["cm"] == 16 and cond["water"]["c"] == 12.7
    assert "waves" not in cond and cond["error"] is None
    assert alert_parts(cond, now) == ["Wind SW 9 m/s (gusts 12)", "visibility 18 km (mist)"]
    # Old observations are shown but not quoted in alerts.
    assert alert_parts(cond, now + 3 * 3600) == []


def test_failures_keep_old_values():
    now = float(int(time.time()))
    weather = SeaWeather(None, fake_fmi(now))
    asyncio.run(weather.refresh())
    weather.fetch = fake_fmi(now, fail=(100997, 100996, 132310, 134221))
    asyncio.run(weather.refresh())
    cond = weather.conditions()
    assert cond["wind"]["ms"] == 8.6 and cond["error"] == "blocked"
    assert weather.start_refresh() is None  # retried after a short pause, not at once
    assert 400 < time.time() - weather.fetched < 600
    # One station failing still counts as a successful refresh.
    weather.fetch = fake_fmi(now, fail=(134221,))
    asyncio.run(weather.refresh())
    assert weather.error is None


def test_alert_waits_only_briefly():
    async def slow(params):
        await asyncio.sleep(5)
        return answer()

    async def run():
        weather = SeaWeather(None, slow)
        started = time.monotonic()
        cond = await weather.latest(wait=0.1)
        waited = time.monotonic() - started
        weather._task.cancel()
        return cond, waited

    cond, waited = asyncio.run(run())
    assert waited < 1 and "wind" not in cond


def test_words_and_numbers():
    assert weather_word(0) is None and weather_word(31) == "fog" and weather_word(63) == "rain"
    assert visibility_text(0.42) == "400 m" and visibility_text(4.53) == "4.5 km" and visibility_text(31.2) == "31 km"


def test_sun_times_helsinki():
    # Published times for Helsinki: 21 June 03:54 / 22:50 (EEST), 21 December 09:24 / 15:13 (EET).
    def hhmm(ts, offset):
        return time.strftime("%H:%M", time.gmtime(ts + offset * 3600))
    june, dec = sun_times(60.17, 24.94, date(2026, 6, 21)), sun_times(60.17, 24.94, date(2026, 12, 21))
    for got, want, offset in ((june["rise"], "03:54", 3), (june["set"], "22:50", 3),
                              (dec["rise"], "09:24", 2), (dec["set"], "15:13", 2)):
        g, w = hhmm(got, offset), want
        assert abs((int(g[:2]) * 60 + int(g[3:])) - (int(w[:2]) * 60 + int(w[3:]))) <= 3, (g, w)
    assert sun_times(78.2, 15.6, date(2026, 6, 21))["set"] is None  # Svalbard: midnight sun


def test_conditions_line():
    now = 1_800_000_000
    cond = {"wind": {"ms": 9.2, "gust": 10.0, "dir": 225, "ts": now - 300},
            "visibility": {"km": 20.0, "weather": None, "ts": now - 300}}
    sun = {"rise": now - 3600, "set": now + 3600}
    expected_set = time.strftime("%H:%M", time.localtime(now + 3600))
    assert conditions_line(cond, sun, now) == f"Wind SW 9 m/s, visibility 20 km, sunset {expected_set}."
    early = {"rise": now + 1800, "set": now + 9 * 3600}
    assert conditions_line(None, early, now) == f"Sunrise {time.strftime('%H:%M', time.localtime(now + 1800))}."
