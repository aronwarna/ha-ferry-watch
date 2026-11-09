"""Inner route outlook: how likely each ferry is to take the inner route past Porkkala on
its next sailings, from the passages seen so far and the weather (wind, gusts, waves, ice).

An experiment, and it says how well it does. The model is a small logistic regression
(pure Python, a dozen numbers), refitted when new passages come in. Before it is shown it
is tested the honest way, as if it had been running all along: for each day of the
history it is fitted only on the passages before that day and asked about that day's
passages, and the answers are scored against what the ships really did, next to the
plain guess "the ship's usual share". If the model can't beat that guess, the panel says
so and shows the usual share instead.

Weather comes from one place for the past and the future alike, so the model is applied
to the same kind of numbers it learned from: Open-Meteo's hourly wind at sea off Porkkala
and its wave model a little further out (both free for non-commercial use, CC BY 4.0),
the last 92 days and the next 3 in two small requests every 3 hours, older days once in
monthly pieces. About 9,000 rows a year in the database.

The next sailings are read off the history too: each ship's passages each way repeat
every day or every other day at about the same time, and a ship already under way has
its time from the next-passage prediction.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import statistics
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .forecast import http_getters
from .store import Store

_LOGGER = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HISTORY_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
WIND_AT = (59.90, 24.35)  # open water just south of Porkkala
WAVES_AT = (59.80, 24.30)  # a little further out, where the wave model has sea around it
PAST_DAYS = 92  # the most Open-Meteo's forecast API gives back
AHEAD_DAYS = 3
REFRESH = 3 * 3600
RETRY = 20 * 60
BACKFILL_DAYS = 30  # one older piece per refresh
REFIT_EVERY = 6 * 3600  # at most, and only when there are new passages
CREDIT = "Weather: Open-Meteo.com (CC BY 4.0)"

HORIZON = 48 * 3600  # sailings this far ahead are shown
WEEK = 7 * 86400
HABIT_SPAN = WEEK  # the ship's own recent choices
FLEET_SPAN = 2 * 86400  # every ship's choices lately
PRIOR = 4.0  # passages' worth of the overall share mixed into a ship's usual share
LAMBDA = 2.0  # L2 penalty on the standardised weights
MIN_TRAIN = 30  # passages before a model is fitted
MIN_EACH = 4  # ...with at least this many of each route
MAX_FOLDS = 60  # refits in the test; more days share a fit
TRAIN_DAYS = 365  # older passages aren't used

NUMERIC = ["wind", "gust", "from_e", "from_n", "wave", "ice", "habit", "fleet"]
# Factors shown as reasons, made of these features.
FACTORS = {"wind": ("wind", "gust"), "direction": ("from_e", "from_n"), "waves": ("wave",), "ice": ("ice",),
           "habit": ("habit", "fleet")}

GetJson = Callable[[str, dict[str, str]], Awaitable[Any]]

SCHEMA = """
CREATE TABLE IF NOT EXISTS wx_hours (
    ts INTEGER PRIMARY KEY,
    ms REAL,
    gust REAL,
    dir REAL,
    wave REAL,
    wdir REAL
);
"""


# --- weather hours ------------------------------------------------------------------------

def parse_hourly(answer: Any, columns: dict[str, str]) -> dict[int, dict[str, float]]:
    """{hour ts: {our name: value}} from an Open-Meteo answer with unixtime times."""
    hourly = (answer or {}).get("hourly") or {}
    out: dict[int, dict[str, float]] = {}
    for i, ts in enumerate(hourly.get("time") or []):
        for ours, theirs in columns.items():
            column = hourly.get(theirs) or []
            v = column[i] if i < len(column) else None
            if v is not None:
                out.setdefault(int(ts), {})[ours] = float(v)
    return out


WIND_COLUMNS = {"ms": "wind_speed_10m", "gust": "wind_gusts_10m", "dir": "wind_direction_10m"}
WAVE_COLUMNS = {"wave": "wave_height", "wdir": "wave_direction"}


class WeatherHours:
    """The hourly table, filled from Open-Meteo."""

    def __init__(self, store: Store, get_json: GetJson):
        self.store = store
        self.get_json = get_json
        store.db.executescript(SCHEMA)
        self.error: str | None = None
        self.updated = 0

    def save(self, hours: dict[int, dict[str, float]]) -> None:
        db = self.store.db
        for ts, v in hours.items():
            db.execute("INSERT INTO wx_hours (ts) VALUES (?) ON CONFLICT(ts) DO NOTHING", (ts,))
            cols = [c for c in ("ms", "gust", "dir", "wave", "wdir") if c in v]
            if cols:
                db.execute(f"UPDATE wx_hours SET {', '.join(f'{c} = ?' for c in cols)} WHERE ts = ?",
                           (*(v[c] for c in cols), ts))
        db.commit()

    def rows(self, since: int, until: int) -> dict[int, tuple]:
        return {r[0]: tuple(r[1:]) for r in self.store.db.execute(
            "SELECT ts, ms, gust, dir, wave, wdir FROM wx_hours WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (since, until))}

    def oldest(self) -> int | None:
        return self.store.db.execute("SELECT MIN(ts) FROM wx_hours WHERE ms IS NOT NULL").fetchone()[0]

    async def _get(self, url: str, at: tuple[float, float], columns: dict[str, str], extra: dict[str, str]):
        params = {"latitude": f"{at[0]:.2f}", "longitude": f"{at[1]:.2f}", "hourly": ",".join(columns.values()),
                  "timeformat": "unixtime", "timezone": "GMT", **extra}
        if columns is WIND_COLUMNS:
            params["wind_speed_unit"] = "ms"
        return parse_hourly(await self.get_json(url, params), columns)

    async def refresh(self, now: float) -> None:
        """The last 92 days and the next 3; waves may fail on their own."""
        extra = {"past_days": str(PAST_DAYS), "forecast_days": str(AHEAD_DAYS)}
        wind = await self._get(FORECAST_URL, WIND_AT, WIND_COLUMNS, extra)
        if not wind:
            raise ValueError("no wind hours in the answer")
        self.save(wind)
        try:
            self.save(await self._get(MARINE_URL, WAVES_AT, WAVE_COLUMNS, extra))
        except Exception as err:  # noqa: BLE001 - wind alone still makes a forecast
            _LOGGER.info("Outlook: no waves from Open-Meteo (%s)", err)
        self.updated = int(now)

    async def backfill(self, wanted_from: int, now: float) -> bool:
        """One older piece (BACKFILL_DAYS) before what is stored, back to wanted_from;
        True if there's more to do."""
        oldest = self.oldest()
        if oldest is None or oldest <= wanted_from + 3600:
            return False
        end = oldest - 3600
        start = max(wanted_from, end - BACKFILL_DAYS * 86400)
        day = lambda ts: time.strftime("%Y-%m-%d", time.gmtime(ts))  # noqa: E731
        extra = {"start_date": day(start), "end_date": day(end)}
        wind = await self._get(HISTORY_URL, WIND_AT, WIND_COLUMNS, extra)
        if not wind:
            raise ValueError("no older wind hours")
        self.save(wind)
        try:
            self.save(await self._get(MARINE_URL, WAVES_AT, WAVE_COLUMNS, extra))
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("Outlook: no older waves (%s)", err)
        return start > wanted_from


def weather_at(hours: dict[int, tuple], ts: float) -> dict[str, float | None]:
    """Wind, gust, direction and waves at a time, between the two nearest hours."""
    h0 = int(ts // 3600 * 3600)
    a, b = hours.get(h0), hours.get(h0 + 3600)
    if a is None and b is None:
        return {}
    if a is None or b is None:
        row = a or b
        return dict(zip(("ms", "gust", "dir", "wave", "wdir"), row))
    f = (ts - h0) / 3600
    out: dict[str, float | None] = {}
    for i, name in enumerate(("ms", "gust", "dir", "wave", "wdir")):
        x, y = a[i], b[i]
        if x is None or y is None:
            out[name] = x if y is None else y
        elif name in ("dir", "wdir"):
            d = ((y - x + 540) % 360) - 180
            out[name] = (x + f * d) % 360
        else:
            out[name] = x + f * (y - x)
    return out


# --- features -----------------------------------------------------------------------------

def group(p: dict[str, Any]) -> str:
    return f"{p['mmsi']}:{p['direction']}"


def recent_share(passages: list[dict[str, Any]], since: float, until: float, base: float,
                 keep: Callable[[dict[str, Any]], bool] = lambda p: True) -> float:
    """Inner share of the passages in [since, until), pulled towards `base`."""
    seen = [p for p in passages if since <= p["ts"] < until and keep(p)]
    inner = sum(p["route"] == "inner" for p in seen)
    return (inner + 2 * base) / (len(seen) + 2)


def features(p: dict[str, Any], wx: dict[str, Any], history: list[dict[str, Any]], issued: float,
             base: float, ice: str | None) -> dict[str, float | None]:
    """The numbers the model sees for one sailing; `history` is what was known at `issued`
    (passages before it), so a test never peeks at the day it is scored on."""
    ms, d = wx.get("ms"), wx.get("dir")
    g = group(p)
    return {
        "wind": ms,
        "gust": wx.get("gust"),
        "from_e": None if ms is None or d is None else ms * math.sin(math.radians(d)),
        "from_n": None if ms is None or d is None else ms * math.cos(math.radians(d)),
        "wave": wx.get("wave"),
        "ice": None if ice is None else float(ice == "ice"),
        "habit": recent_share(history, issued - HABIT_SPAN, issued, base, lambda q: group(q) == g),
        "fleet": recent_share(history, issued - FLEET_SPAN, issued, base),
    }


# --- the model ----------------------------------------------------------------------------

def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """a x = b by Gaussian elimination with partial pivoting (a is small and well-posed)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        if abs(m[c][c]) < 1e-12:
            continue
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            if f:
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        if abs(m[r][r]) < 1e-12:
            continue
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def _sigmoid(z: float) -> float:
    if z < -30:
        return 1e-13
    if z > 30:
        return 1 - 1e-13
    return 1 / (1 + math.exp(-z))


class Model:
    """Logistic regression: a weight per ship and direction (shrunk towards the overall
    share), and per standardised weather/habit number; missing numbers count as average."""

    def __init__(self) -> None:
        self.mean: dict[str, float] = {}
        self.sd: dict[str, float] = {}
        self.groups: list[str] = []
        self.w: list[float] = []
        self.n = 0
        self.inner = 0

    def _row(self, f: dict[str, float | None], g: str) -> list[float]:
        x = [1.0]
        for name in NUMERIC:
            v = f.get(name)
            x.append(0.0 if v is None else (v - self.mean[name]) / self.sd[name])
        x.extend(1.0 if g == k else 0.0 for k in self.groups)
        return x

    def fit(self, rows: list[tuple[dict[str, float | None], str, int]]) -> "Model":
        self.n, self.inner = len(rows), sum(y for _, _, y in rows)
        for name in NUMERIC:
            vals = [f[name] for f, _, _ in rows if f.get(name) is not None]
            self.mean[name] = statistics.fmean(vals) if vals else 0.0
            sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            self.sd[name] = sd if sd > 1e-6 else 1.0
        self.groups = sorted({g for _, g, _ in rows})
        xs = [self._row(f, g) for f, g, _ in rows]
        ys = [y for _, _, y in rows]
        k = len(xs[0])
        share = (self.inner + 1) / (self.n + 2)
        w = [math.log(share / (1 - share))] + [0.0] * (k - 1)
        for _ in range(30):
            grad = [0.0] * k
            hess = [[0.0] * k for _ in range(k)]
            for x, y in zip(xs, ys):
                p = _sigmoid(sum(a * b for a, b in zip(w, x)))
                r, s = p - y, p * (1 - p)
                nz = [i for i, v in enumerate(x) if v]
                for i in nz:
                    grad[i] += r * x[i]
                    si = s * x[i]
                    row = hess[i]
                    for j in nz:
                        row[j] += si * x[j]
            for i in range(1, k):
                grad[i] += LAMBDA * w[i]
                hess[i][i] += LAMBDA
            step = _solve(hess, grad)
            w = [a - b for a, b in zip(w, step)]
            if max(abs(s) for s in step) < 1e-6:
                break
        self.w = w
        return self

    def logit(self, f: dict[str, float | None], g: str) -> float:
        return sum(a * b for a, b in zip(self.w, self._row(f, g)))

    def chance(self, f: dict[str, float | None], g: str) -> float:
        return _sigmoid(self.logit(f, g))

    def reasons(self, f: dict[str, float | None]) -> list[tuple[str, float]]:
        """(factor, push in log-odds against an average sailing), strongest first."""
        x = self._row(f, "")
        index = {name: 1 + i for i, name in enumerate(NUMERIC)}
        out = [(factor, sum(self.w[index[n]] * x[index[n]] for n in names)) for factor, names in FACTORS.items()]
        return sorted(out, key=lambda t: -abs(t[1]))


def usual_shares(passages: list[dict[str, Any]]) -> tuple[float, dict[str, float]]:
    """(overall inner share, {ship and direction: its share pulled towards the overall})."""
    inner = sum(p["route"] == "inner" for p in passages)
    base = (inner + 1) / (len(passages) + 2)
    counts: dict[str, list[int]] = {}
    for p in passages:
        c = counts.setdefault(group(p), [0, 0])
        c[0] += p["route"] == "inner"
        c[1] += 1
    return base, {g: (i + PRIOR * base) / (n + PRIOR) for g, (i, n) in counts.items()}


def can_fit(passages: list[dict[str, Any]]) -> bool:
    inner = sum(p["route"] == "inner" for p in passages)
    return len(passages) >= MIN_TRAIN and inner >= MIN_EACH and len(passages) - inner >= MIN_EACH


def _rows_fast(passages: list[dict[str, Any]], hours: dict[int, tuple]) -> list[tuple]:
    """training_rows with the habit windows found by moving pointers (passages sorted by ts),
    and the overall share as it stood each midnight."""
    out = []
    inner_before = 0
    j = 0
    for p in passages:
        issued = p["ts"] - p["ts"] % 86400
        while j < len(passages) and passages[j]["ts"] < issued:
            inner_before += passages[j]["route"] == "inner"
            j += 1
        base = (inner_before + 1) / (j + 2)
        lo = j
        while lo > 0 and passages[lo - 1]["ts"] >= issued - HABIT_SPAN:
            lo -= 1
        window = passages[lo:j]
        f = features(p, weather_at(hours, p["ts"]), window, issued, base, p.get("ice"))
        out.append((f, group(p), int(p["route"] == "inner")))
    return out


# --- the honest test ----------------------------------------------------------------------

def backtest(passages: list[dict[str, Any]], hours: dict[int, tuple]) -> dict[str, Any]:
    """Walk forward through the history a day at a time: fit on the passages before the
    day (refitting at most MAX_FOLDS times), score that day's passages. Returns the scores
    of the model and of the plain usual share, side by side."""
    passages = sorted(passages, key=lambda p: p["ts"])
    rows = _rows_fast(passages, hours)
    days = sorted({p["ts"] - p["ts"] % 86400 for p in passages})
    first = next((i for i, d in enumerate(days) if can_fit([p for p in passages if p["ts"] < d])), None)
    scored: list[tuple[float, float, int]] = []  # (model, usual, happened)
    if first is not None:
        test_days = days[first:]
        every = max(1, math.ceil(len(test_days) / MAX_FOLDS))
        model = None
        usual: dict[str, float] = {}
        base = 0.0
        for n, day in enumerate(test_days):
            if n % every == 0:
                train = [(r, p) for r, p in zip(rows, passages) if day - TRAIN_DAYS * 86400 <= p["ts"] < day]
                model = Model().fit([r for r, _ in train])
                base, usual = usual_shares([p for _, p in train])
            for r, p in zip(rows, passages):
                if day <= p["ts"] < day + 86400:
                    scored.append((model.chance(r[0], r[1]), usual.get(group(p), base), r[2]))
    return score(scored, len(passages))


def score(scored: list[tuple[float, float, int]], total: int) -> dict[str, Any]:
    n = len(scored)
    out: dict[str, Any] = {"passages": total, "tested": n, "inner": sum(y for _, _, y in scored)}
    if not n:
        out["verdict"] = "untested"
        return out
    brier = statistics.fmean((p - y) ** 2 for p, _, y in scored)
    brier_usual = statistics.fmean((u - y) ** 2 for _, u, y in scored)
    skill = 1 - brier / brier_usual if brier_usual > 0 else 0.0
    out.update(brier=round(brier, 4), brier_usual=round(brier_usual, 4), skill=round(skill, 3))
    # Said "likely" (50% or more): how many inner passages were caught, how many false alarms.
    out["caught"] = sum(1 for p, _, y in scored if p >= 0.5 and y)
    out["false_alarms"] = sum(1 for p, _, y in scored if p >= 0.5 and not y)
    out["caught_usual"] = sum(1 for _, u, y in scored if u >= 0.5 and y)
    # Mean chance given before inner and before outer passages.
    inner_p = [p for p, _, y in scored if y]
    outer_p = [p for p, _, y in scored if not y]
    out["mean_before_inner"] = round(statistics.fmean(inner_p), 3) if inner_p else None
    out["mean_before_outer"] = round(statistics.fmean(outer_p), 3) if outer_p else None
    # Calibration: chances in five bands, how often inner really followed.
    bands = []
    for lo, hi in ((0, .1), (.1, .25), (.25, .5), (.5, .75), (.75, 1.01)):
        inside = [(p, y) for p, _, y in scored if lo <= p < hi]
        if inside:
            bands.append([lo, min(hi, 1), len(inside), sum(y for _, y in inside),
                          round(statistics.fmean(p for p, _ in inside), 3)])
    out["bands"] = bands
    if n < 30 or out["inner"] < 5:
        out["verdict"] = "too_little"
    elif skill >= 0.10:
        out["verdict"] = "better"
    elif skill >= 0.02:
        out["verdict"] = "slightly_better"
    elif skill > -0.02:
        out["verdict"] = "same"
    else:
        out["verdict"] = "worse"
    return out


# --- the next sailings ---------------------------------------------------------------------

def next_sailings(passages: list[dict[str, Any]], now: float, live: dict[int, dict[str, Any]],
                  horizon: float = HORIZON) -> list[dict[str, Any]]:
    """[{mmsi, direction, ts, live}] in (now - 30 min, now + horizon), from each ship's
    rhythm each way over the last three weeks, and the live predictions."""
    out = []
    by_group: dict[tuple[int, str], list[float]] = {}
    for p in passages:
        if p["ts"] >= now - 21 * 86400:
            by_group.setdefault((p["mmsi"], p["direction"]), []).append(p.get("porkkala_ts") or p["ts"])
    for (mmsi, direction), times in by_group.items():
        times.sort()
        if len(times) < 2:
            continue
        gaps = [(b - a) / 86400 for a, b in zip(times, times[1:]) if b - a > 6 * 3600]
        if not gaps:
            continue
        period = max(1, round(statistics.median(gaps)))
        last = times[-1]
        if now - last > 3 * period * 86400 + 86400:
            continue  # stopped running (yard, another route)
        # Time of day: the median of the last few, around the last one's.
        tods = [((t - last + 43200) % 86400) - 43200 for t in times[-5:]]
        shift = statistics.median(tods)
        k = 1
        while True:
            ts = last + shift + k * period * 86400
            if ts > now + horizon:
                break
            if ts > now - 1800:
                out.append({"mmsi": mmsi, "direction": direction, "ts": int(ts), "live": False})
            k += 1
    for mmsi, pred in live.items():
        if not pred or not pred.get("direction") or not (now - 1800 < pred["eta_ts"] < now + horizon):
            continue
        same = [s for s in out if s["mmsi"] == mmsi and s["direction"] == pred["direction"]
                and abs(s["ts"] - pred["eta_ts"]) < 8 * 3600]
        for s in same:
            out.remove(s)
        out.append({"mmsi": mmsi, "direction": pred["direction"], "ts": int(pred["eta_ts"]), "live": True})
    return sorted(out, key=lambda s: s["ts"])


# --- the service ---------------------------------------------------------------------------

class Outlook:
    def __init__(self, store: Store, session: aiohttp.ClientSession | None, get_json: GetJson | None = None,
                 live: Callable[[int], dict[str, Any] | None] | None = None, ice: Any = None):
        self.store = store
        self.hours = WeatherHours(store, get_json or http_getters(session)[1])
        self.live = live or (lambda mmsi: None)
        self.ice = ice
        self.model: Model | None = None
        self.test: dict[str, Any] = {"verdict": "untested"}
        self.fitted = 0
        self._fitted_count = -1
        self._fitting = False

    def _passages(self) -> list[dict[str, Any]]:
        mmsis = [s["mmsi"] for s in self.store.ships()]
        if not mmsis:
            return []
        since = int(time.time() - TRAIN_DAYS * 86400)
        rows = self.store.db.execute(
            f"SELECT mmsi, ts, route, direction, ice, porkkala_ts FROM passages WHERE ts >= ? "
            f"AND mmsi IN ({','.join('?' * len(mmsis))}) AND route IN ('inner', 'outer') ORDER BY ts",
            (since, *mmsis)).fetchall()
        return [dict(r) for r in rows]

    async def run(self) -> None:
        first = True
        while True:
            wait, fresh = REFRESH, False
            try:
                await self.refresh()
                fresh = True
            except Exception as err:  # noqa: BLE001 - try again later
                self.hours.error = str(err) or type(err).__name__
                _LOGGER.warning("Outlook: weather not refreshed: %s", self.hours.error)
                wait = RETRY
            try:
                await self.refit(force=first and fresh)
                first = first and not fresh
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Outlook: the model could not be fitted")
            await asyncio.sleep(wait)

    async def refresh(self) -> None:
        now = time.time()
        await self.hours.refresh(now)
        self.hours.error = None
        passages = self._passages()
        if passages:
            for _ in range(3):  # a few older pieces each time, so a long history fills in a day
                if not await self.hours.backfill(passages[0]["ts"] - 3600, now):
                    break

    async def refit(self, force: bool = False) -> None:
        """Fit and test again: when forced (new weather hours), else when there are new
        passages and the last fit is REFIT_EVERY old."""
        passages = self._passages()
        if self._fitting:
            return
        if not force and self._fitted_count >= 0 and (
                len(passages) == self._fitted_count or time.time() - self.fitted < REFIT_EVERY):
            return
        self._fitting = True
        try:
            hours = self.hours.rows(passages[0]["ts"] - 7200, int(time.time())) if passages else {}
            model, test = await asyncio.to_thread(self._fit, passages, hours)
            self.model, self.test = model, test
            self.fitted, self._fitted_count = int(time.time()), len(passages)
            _LOGGER.info("Outlook: fitted on %s passages; test %s", len(passages),
                         {k: test.get(k) for k in ("tested", "inner", "skill", "verdict")})
        finally:
            self._fitting = False

    @staticmethod
    def _fit(passages: list[dict[str, Any]], hours: dict[int, tuple]) -> tuple[Model | None, dict[str, Any]]:
        test = backtest(passages, hours)
        if not can_fit(passages):
            return None, test
        return Model().fit([r for r in _rows_fast(passages, hours)]), test

    async def payload(self, ships: dict[int, dict[str, Any]], now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        passages = self._passages()
        base, usual = usual_shares(passages) if passages else (0.0, {})
        live = {m: self.live(m) for m in ships}
        hours = self.hours.rows(int(now - 86400), int(now + HORIZON + 7200))
        ice_state = None
        if self.ice:
            try:
                day = await self.ice.current(now)
                ice_state = day and day.get("state")
            except Exception:  # noqa: BLE001
                ice_state = None
        use_model = self.model is not None and self.test.get("verdict") in ("better", "slightly_better", "too_little",
                                                                             "same")
        sailings = []
        for s in next_sailings(passages, now, {m: p for m, p in live.items() if p}):
            if s["mmsi"] not in ships:
                continue
            g = group(s)
            wx = weather_at(hours, s["ts"])
            entry = {**s, "usual": round(usual.get(g, base), 3),
                     "wind": None if wx.get("ms") is None else round(wx["ms"], 1),
                     "gust": None if wx.get("gust") is None else round(wx["gust"], 1),
                     "dir": None if wx.get("dir") is None else round(wx["dir"]),
                     "wave": None if wx.get("wave") is None else round(wx["wave"], 2)}
            if use_model and wx:
                f = features(s, wx, passages, now, base, ice_state)
                entry["chance"] = round(self.model.chance(f, g), 3)
                entry["reasons"] = [[k, round(v, 2)] for k, v in self.model.reasons(f) if abs(v) >= 0.15][:3]
            sailings.append(entry)
        return {"sailings": sailings, "test": self.test, "fitted": self.fitted,
                "model": None if not self.model else {"passages": self.model.n, "inner": self.model.inner},
                "using": "model" if use_model else "usual", "base": round(base, 3),
                "weather": {"updated": self.hours.updated, "error": self.hours.error, "credit": CREDIT}}


# --- simulator ----------------------------------------------------------------------------

def fake_get_json(store: Store) -> GetJson:
    """Open-Meteo stand-in (FW_SIMULATE): the made-up history's stored winds for the past,
    a wandering wind for the days ahead, waves following the wind."""
    rng = random.Random(3)

    async def get_json(url: str, params: dict[str, str]) -> Any:
        now = time.time()
        if "start_date" in params:
            start = time.mktime(time.strptime(params["start_date"], "%Y-%m-%d"))
            end = time.mktime(time.strptime(params["end_date"], "%Y-%m-%d")) + 86400
        else:
            start = now - int(params.get("past_days", 0)) * 86400
            end = now + int(params.get("forecast_days", 0)) * 86400
        start, end = int(start // 3600 * 3600), int(end // 3600 * 3600)
        known = {r[0] - r[0] % 3600: (r[1], r[2], r[3]) for r in store.db.execute(
            "SELECT ts, wind_ms, gust_ms, wind_dir FROM passages WHERE wind_ms IS NOT NULL AND ts BETWEEN ? AND ?",
            (start - 7200, end))}
        times, ms, gust, dirs, wave, wdir = [], [], [], [], [], []
        w, d = 6.0, 220.0
        for ts in range(start, end, 3600):
            if ts in known:
                w, g, d = known[ts]
                d = d if d is not None else 220.0
            else:
                w = max(0.5, w + rng.gauss(0, 0.8) - 0.03 * (w - 7))
                d = (d + rng.gauss(0, 12)) % 360
                g = w * 1.4
            times.append(ts)
            ms.append(round(w, 1))
            gust.append(round(g or w * 1.4, 1))
            dirs.append(round(d))
            wave.append(round(0.12 * w + 0.01 * w * w, 2))
            wdir.append(round(d))
        if "marine" in url:
            return {"hourly": {"time": times, "wave_height": wave, "wave_direction": wdir}}
        return {"hourly": {"time": times, "wind_speed_10m": ms, "wind_gusts_10m": gust,
                           "wind_direction_10m": dirs}}

    return get_json


def simulated() -> bool:
    return os.environ.get("FW_SIMULATE") == "1"
