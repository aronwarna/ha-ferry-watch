"""The boating day score: sampling a route, the forecast along it, and the hourly score."""

import asyncio
import time

import aiohttp
import pytest

from ferrywatch import dayscore, marine
from ferrywatch.dayscore import BOATS, Series, sample, score_hours, shelter
from ferrywatch.geo import distance_m
from ferrywatch.simweather import fake_fmi_fetch, fake_marine_json

ROUTE = [[60.15, 24.95], [60.10, 24.70], [59.97, 24.45]]


def test_sample_spacing_and_ends():
    pts = sample(ROUTE)
    assert pts[0][:2] == (60.15, 24.95) and pts[-1][:2] == (59.97, 24.45)
    total = sum(distance_m(*a, *b) for a, b in zip(ROUTE, ROUTE[1:]))
    assert abs(pts[-1][2] - total) < 1
    gaps = [b[2] - a[2] for a, b in zip(pts, pts[1:])]
    assert max(gaps) <= dayscore.SAMPLE_M + 1
    # A long route gets no more than MAX_SAMPLES points.
    assert len(sample([[60, 20], [60, 28]])) == dayscore.MAX_SAMPLES


def _series(now, n, wind, wave=0.2, rain=0.0):
    times = dayscore.hours(now)
    air = Series(times, dayscore.AIR, n)
    sea = Series(times, dayscore.SEA, n)
    for i in range(n):
        for t in times:
            air.set(i, t, "ms", wind(i, t))
            air.set(i, t, "gust", wind(i, t) * 1.4)
            air.set(i, t, "rain", rain)
            air.set(i, t, "vis", 20)
            air.set(i, t, "t", 8)
            sea.set(i, t, "wave", wave)
    sea.extra["cell_km"] = [1.0] * n
    return air, sea


def test_worst_place_on_the_way_decides_and_says_where():
    now = 1786000000.0  # 2026-08-06 07:06 UTC: daylight in Finland
    pts = sample(ROUTE)
    n = len(pts)
    # Calm, but a windy spot two thirds along.
    air, sea = _series(now, n, lambda i, t: 11.0 if i == (2 * n) // 3 else 3.0)
    out = score_hours(pts, air, sea, 25, "medium", now)
    h = out["hours"][0]
    assert h["score"] == 2 and h["wind"] == 11.0
    assert h["levels"][(2 * n) // 3] == "2" and h["levels"][0] == "0"
    wind = next(w for w in h["why"] if w["k"] == "wind")
    assert wind["lvl"] == 2 and wind["nm"] == round(pts[(2 * n) // 3][2] / 1852, 1)
    # A bigger boat takes it as okay.
    assert score_hours(pts, air, sea, 25, "large", now)["hours"][0]["score"] == 1


def test_wind_later_reaches_a_slow_boat_but_not_a_fast_one():
    now = 1786000000.0
    start = int(now // 3600 * 3600)
    pts = sample(ROUTE)
    # Gale from an hour after the start of the first hour.
    air, sea = _series(now, len(pts), lambda i, t: 3.0 if t <= start + 3600 else 14.0)
    fast = score_hours(pts, air, sea, 30, "medium", now)["hours"][0]  # ~40 min trip
    slow = score_hours(pts, air, sea, 6, "medium", now)["hours"][0]  # ~3 h trip
    assert fast["score"] == 0 and slow["score"] == 2


def test_darkness_makes_an_hour_okay_at_best():
    pts = sample(ROUTE)
    night = 1786060800.0  # 2026-08-07 00:00 UTC, 03:00 in Helsinki
    air, sea = _series(night, len(pts), lambda i, t: 2.0)
    h = score_hours(pts, air, sea, 25, "medium", night)["hours"][0]
    assert h["dark"] and h["score"] == 1 and h["why"][0]["k"] == "dark"


def test_waves_count_less_where_the_model_sea_is_far():
    assert shelter(None) == 1 and shelter(1.5) == 1
    assert shelter(8) == pytest.approx(0.5) and shelter(30) == pytest.approx(0.5)
    assert 0.5 < shelter(5) < 1
    now = 1786000000.0
    pts = sample(ROUTE)
    air, sea = _series(now, len(pts), lambda i, t: 3.0, wave=1.2)
    assert score_hours(pts, air, sea, 25, "medium", now)["hours"][0]["score"] == 2
    sea.extra["cell_km"] = [10.0] * len(pts)
    h = score_hours(pts, air, sea, 25, "medium", now)["hours"][0]
    assert h["score"] == 1 and h["wave"] == 0.6 and h["sheltered"]


def test_trips_longer_than_the_forecast_have_no_hours():
    now = 1786000000.0
    pts = sample([[60, 20], [60, 28]])  # ~240 NM
    air, sea = _series(now, len(pts), lambda i, t: 3.0)
    assert score_hours(pts, air, sea, 2, "medium", now)["hours"] == []
    assert len(score_hours(pts, air, sea, 40, "medium", now)["hours"]) > 30


async def _sea_json(url, params):
    if url == marine.URL:
        return await fake_marine_json(url, params)
    raise ValueError("offline")


def test_day_score_from_the_point_forecasts_and_cached():
    calls = []

    async def fetch(url, params):
        calls.append(url)
        return await fake_fmi_fetch(url, params)

    async def main():
        d = dayscore.DayScore(None, None, fetch, _sea_json)
        out = await d.score(ROUTE, 25)
        assert out["sources"] == {"air": "fmi", "sea": "open-meteo"} and out["errors"] == {}
        assert len(out["hours"]) >= 46 and all(h["score"] in (0, 1, 2) for h in out["hours"])
        assert all(len(h["levels"]) == len(out["samples"]) for h in out["hours"])
        assert {"wind", "gust", "wave", "rain"} <= set(out["hours"][0])
        await d.score(ROUTE, 10, "small")  # same route: from memory
        assert len(calls) == 1

    asyncio.run(main())


def test_fmi_refusing_falls_back_to_open_meteo():
    async def refuse(url, params):
        raise aiohttp.ClientError("HTTP 503")

    async def om(url, params):
        if url == marine.URL:
            return await fake_marine_json(url, params)
        lats = params["latitude"].split(",")
        start = int(time.time() // 3600 * 3600)
        times = [start + i * 3600 for i in range(49)]
        place = {"hourly": {"time": times, "wind_speed_10m": [4.0] * 49, "wind_gusts_10m": [6.0] * 49,
                            "precipitation": [0.0] * 49, "visibility": [20000] * 49}}
        return [place] * len(lats)

    async def main():
        d = dayscore.DayScore(None, None, refuse, om)
        out = await d.score(ROUTE, 25)
        assert out["sources"]["air"] == "open-meteo" and "fmi" in out["errors"]
        assert out["hours"][0]["wind"] == 4.0

    asyncio.run(main())
