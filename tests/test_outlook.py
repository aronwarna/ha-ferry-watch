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


def _outlook(tmp_path):
    store = Store(tmp_path / "t.db")

    async def get_json(url, params):
        return {"hourly": {"time": []}}

    return store, o.Outlook(store, None, get_json)


def test_recent_passages_count_for_more():
    w = o.recency_weights([0, o.HALF_LIFE, 2 * o.HALF_LIFE], 2 * o.HALF_LIFE)
    assert abs(sum(w) / 3 - 1) < 1e-9 and abs(w[2] / w[1] - 2) < 1e-9 and abs(w[1] / w[0] - 2) < 1e-9
    # A habit that changed: all outer long ago, all inner lately. The weighted fit follows the recent part.
    rows = [({}, "1:west", 0)] * 60 + [({}, "1:west", 1)] * 20
    times = [i * 3 * 86400 for i in range(80)]
    plain = o.Model().fit(rows).chance({}, "1:west")
    weighted = o.Model().fit(rows, o.recency_weights(times, times[-1])).chance({}, "1:west")
    assert weighted > plain + 0.1


def test_log_is_written_matched_and_marked(tmp_path):
    store, out = _outlook(tmp_path)
    now = T0 + 1000
    sailing = {"mmsi": 1, "direction": "west", "ts": now + 20 * 3600, "usual": 0.2, "model": 0.7, "chance": 0.65}
    assert out.log_forecasts([sailing], now) == 1
    # A later estimate of the same sailing moves its row; the half-day number stays as it stood.
    later = {**sailing, "ts": now + 21 * 3600, "model": 0.4, "chance": 0.4}
    out.log_forecasts([later], now + 15 * 3600)
    # Inside the last hour nothing is written any more.
    out.log_forecasts([{**later, "model": 0.99}], now + 20.5 * 3600)
    rows = store.db.execute("SELECT ts, model, model_day, outcome FROM outlook_log").fetchall()
    assert [tuple(r) for r in rows] == [(now + 21 * 3600, 0.4, 0.7, None)]
    store.db.execute("INSERT INTO passages (mmsi, ts, route, direction, lat, lon) VALUES (1, ?, 'inner', 'west', 60, 24.4)",
                     (now + 21 * 3600 + 900,))
    store.db.commit()
    assert out.resolve(now + 22 * 3600) == 1
    assert store.db.execute("SELECT outcome FROM outlook_log").fetchone()[0] == 1
    # A sailing that never comes is set aside, not scored.
    out.log_forecasts([{**sailing, "mmsi": 2, "ts": now + 30 * 3600}], now + 22 * 3600)
    out.resolve(now + 30 * 3600 + o.LOG_GIVE_UP + 60)
    assert store.db.execute("SELECT outcome FROM outlook_log WHERE mmsi = 2").fetchone()[0] == -1
    assert out.live()["logged"] == 1


def test_calibration_pulls_in_an_overconfident_model():
    rng = random.Random(2)
    pairs = []
    for _ in range(400):
        true = rng.random()
        said = o._sigmoid(o._logit(true) * 2.5)  # far too sure
        pairs.append((said, int(rng.random() < true)))
    a, b = o.calibrate(pairs)
    assert 0.25 < b < 0.7 and abs(a) < 0.4
    assert abs(o.corrected(0.95, (a, b)) - 0.5) < abs(0.95 - 0.5)
    assert o.calibrate(pairs[:10]) is None and o.corrected(0.3, None) == 0.3


def test_the_log_has_the_last_word(tmp_path):
    store, out = _outlook(tmp_path)
    out.model = object()
    out.test = {"verdict": "better"}
    assert out.decide(out.live()) == (True, "test")
    rng = random.Random(4)
    for i in range(80):  # a model that says the opposite of what happens
        y = int(rng.random() < 0.3)
        store.db.execute("INSERT INTO outlook_log (mmsi, direction, ts, issued, model, shown, usual, outcome) "
                         "VALUES (1, 'west', ?, ?, ?, ?, 0.3, ?)", (T0 + i * 86400, T0 + i * 86400 - 7200, 0.1 if y else 0.9, 0.5, y))
    store.db.commit()
    live = out.live()
    assert live["tested"] == 80 and live["verdict"] == "worse" and len(live["months"]) >= 2
    assert out.decide(live) == (False, "log")
