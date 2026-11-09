import asyncio
import math
import random

from ferrywatch import outlook as o
from ferrywatch.store import Store

T0 = 1_700_000_000 - 1_700_000_000 % 86400


def history(days, signal, seed=1):
    rng = random.Random(seed)
    hours, out = {}, []
    w, d = 6.0, 220.0
    for h in range(days * 24 + 48):
        w = max(0.5, w + rng.gauss(0, 0.8) - 0.03 * (w - 7))
        d = (d + rng.gauss(0, 12)) % 360
        hours[T0 + h * 3600] = (w, w * 1.4, d, 0.12 * w, d)
    for day in range(days):
        for m, direction, hour in ((1, "west", 17), (2, "east", 8.5), (3, "west", 17.5), (4, "east", 9)):
            if (day + m) % 2:
                direction = "east" if direction == "west" else "west"
            ts = T0 + day * 86400 + int(hour * 3600) + rng.randint(-1800, 1800)
            z = -2.5 + signal * (hours[ts - ts % 3600][0] - 7) / 3
            route = "inner" if rng.random() < 1 / (1 + math.exp(-z)) else "outer"
            out.append({"mmsi": m, "direction": direction, "ts": ts, "route": route, "ice": None, "porkkala_ts": None})
    return out, hours


def test_backtest_finds_a_real_signal():
    passages, hours = history(120, 1.5)
    t = o.backtest(passages, hours)
    assert t["tested"] > 300 and t["verdict"] == "better" and t["skill"] > 0.1


def test_backtest_is_honest_about_noise():
    passages, hours = history(120, 0.0)
    t = o.backtest(passages, hours)
    assert t["verdict"] in ("same", "worse")


def test_too_little_history_is_untested():
    passages, hours = history(5, 1.5)
    assert o.backtest(passages, hours)["verdict"] in ("untested", "too_little")


def test_next_sailings_follow_the_rhythm():
    passages, _ = history(20, 0.0)
    now = passages[-1]["ts"] + 3600
    found = o.next_sailings(passages, now, {})
    assert found and all(now - 1800 < s["ts"] < now + o.HORIZON for s in found)
    # Every ship sails each way every other day: about one per ship and day.
    assert 5 <= len(found) <= 10
    live = o.next_sailings(passages, now, {1: {"eta_ts": now + 600, "direction": "west"}})
    assert any(s["live"] and s["mmsi"] == 1 for s in live)


def test_weather_hours_from_open_meteo(tmp_path):
    store = Store(tmp_path / "t.db")

    async def get_json(url, params):
        if "marine" in url:
            return {"hourly": {"time": [T0, T0 + 3600], "wave_height": [1.0, 2.0], "wave_direction": [200, 220]}}
        return {"hourly": {"time": [T0, T0 + 3600], "wind_speed_10m": [5, 7], "wind_gusts_10m": [8, 10],
                           "wind_direction_10m": [350, 10]}}

    hours = o.WeatherHours(store, get_json)
    asyncio.run(hours.refresh(T0))
    wx = o.weather_at(hours.rows(T0 - 10, T0 + 7200), T0 + 1800)
    assert wx["ms"] == 6 and wx["wave"] == 1.5 and abs(wx["dir"]) % 360 < 1e-6
