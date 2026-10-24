"""Rescue aircraft flight history, kept on disk for a year.

While the Rescue aircraft switch is on, every position adsb.fi reports goes through
`record`, which thins it before storing: a point is kept when 30 s have passed since the
last stored one, the heading turned more than 15° or the altitude changed more than
100 m. Aircraft standing on the ground aren't stored, and 20 minutes without a stored
point start a new flight.

Only the last stored point of each aircraft in the air is held in memory. Flights are
read back one at a time; each finished flight also gets a simplified line (about 50 m
precision, with times) so the overview of a whole year never reads the raw points.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterator

from .geo import angle_diff, distance_m

_LOGGER = logging.getLogger(__name__)

KEEP_DAYS = 365
FLIGHT_GAP = 20 * 60
MIN_INTERVAL = 30  # s between stored points...
TURN_DEGREES = 15  # ...unless the heading turned this much
CLIMB_M = 100  # ...or the altitude changed this much
GROUND_KN = 50  # on the ground and slower than this: parked or taxiing, not stored
LINE_TOLERANCE_M = 50
PERIODS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "year": 365 * 86400}
# Coarser lines for longer periods keep a year's overview to a few hundred kB.
OVERVIEW_TOLERANCE = ((2 * 86400, 0), (10 * 86400, 120), (40 * 86400, 250), (math.inf, 500))
MAX_FLIGHTS = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS air_flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    icao TEXT NOT NULL,
    reg TEXT, callsign TEXT, type TEXT, type_name TEXT, operator TEXT, helicopter INTEGER,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    points INTEGER NOT NULL DEFAULT 0,
    max_alt INTEGER,
    dist_m REAL NOT NULL DEFAULT 0,
    line TEXT
);
CREATE INDEX IF NOT EXISTS air_flights_end ON air_flights (end_ts);
CREATE INDEX IF NOT EXISTS air_flights_icao_end ON air_flights (icao, end_ts);
-- The primary key is the (icao, ts) index; WITHOUT ROWID stores the rows in it.
CREATE TABLE IF NOT EXISTS air_points (
    icao TEXT NOT NULL,
    ts INTEGER NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    alt INTEGER,
    trk INTEGER,
    PRIMARY KEY (icao, ts)
) WITHOUT ROWID;
"""

META = ("reg", "callsign", "type", "type_name", "operator", "helicopter")


@dataclass
class _Last:
    flight: int
    ts: int
    lat: float
    lon: float
    alt: int | None
    trk: float | None


# --- compact lines: Google's polyline encoding with a third value, the time -----------

def _put(value: int, out: list[str]) -> None:
    value = ~(value << 1) if value < 0 else value << 1
    while value >= 0x20:
        out.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    out.append(chr(value + 63))


def encode(points: list[tuple[float, float, int]]) -> str:
    """(lat, lon, ts) points as one string: 1e-4° (about 10 m) and 1 s, as deltas."""
    out: list[str] = []
    prev = (0, 0, 0)
    for lat, lon, ts in points:
        cur = (round(lat * 1e4), round(lon * 1e4), int(ts))
        for a, b in zip(cur, prev):
            _put(a - b, out)
        prev = cur
    return "".join(out)


def decode(text: str) -> list[tuple[float, float, int]]:
    values, shift, result = [], 0, 0
    for ch in text:
        b = ord(ch) - 63
        result |= (b & 0x1F) << shift
        shift += 5
        if b < 0x20:
            values.append(~(result >> 1) if result & 1 else result >> 1)
            shift = result = 0
    out, lat, lon, ts = [], 0, 0, 0
    for i in range(0, len(values) - 2, 3):
        lat, lon, ts = lat + values[i], lon + values[i + 1], ts + values[i + 2]
        out.append((lat / 1e4, lon / 1e4, ts))
    return out


def simplify(points: list[tuple], tolerance_m: float) -> list[tuple]:
    """Douglas-Peucker on (lat, lon, ...) points, in metres; the ends are always kept."""
    if tolerance_m <= 0 or len(points) < 3:
        return list(points)
    kx = 111_320 * math.cos(math.radians(points[0][0]))
    xy = [(p[1] * kx, p[0] * 110_540) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        best, best_i = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            d = (abs(dy * (px - ax) - dx * (py - ay)) / length if length
                 else math.hypot(px - ax, py - ay))
            if d > best:
                best, best_i = d, i
        if best > tolerance_m:
            keep[best_i] = True
            stack += [(a, best_i), (best_i, b)]
    return [p for p, k in zip(points, keep) if k]


def overview_tolerance(seconds: float) -> int:
    return next(tol for limit, tol in OVERVIEW_TOLERANCE if seconds <= limit)


class AircraftHistory:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.last: dict[str, _Last] = {}

    # --- recording -------------------------------------------------------

    def record(self, ac: dict[str, Any], ts: float, commit: bool = True) -> int | None:
        """Store one reported position of an aircraft (an AircraftWatcher entry) if it says
        something new; returns the id of its flight, or None while it is on the ground."""
        icao = str(ac["hex"]).lower()
        ts = int(ts)
        alt_ft = ac.get("alt_ft")
        alt = round(alt_ft * 0.3048) if isinstance(alt_ft, (int, float)) else None
        if alt_ft == 0 and (ac.get("gs_kn") or 0) < GROUND_KN:
            return None
        lat, lon, trk = float(ac["lat"]), float(ac["lon"]), ac.get("track")
        last = self.last.get(icao) or self._resume(icao)
        if last and ts <= last.ts:
            return last.flight
        if last and ts - last.ts <= FLIGHT_GAP:
            turned = trk is not None and last.trk is not None and angle_diff(trk, last.trk) > TURN_DEGREES
            climbed = alt is not None and last.alt is not None and abs(alt - last.alt) > CLIMB_M
            if ts - last.ts < MIN_INTERVAL and not turned and not climbed:
                return last.flight
            step = distance_m(last.lat, last.lon, lat, lon)
            self.db.execute(
                "UPDATE air_flights SET end_ts = ?, points = points + 1, dist_m = dist_m + ?, "
                "max_alt = max(coalesce(max_alt, ?), ?) WHERE id = ?",
                (ts, step, alt, alt, last.flight))
            self._fill_meta(last.flight, ac)
            flight = last.flight
        else:
            if last:
                self.finish(last.flight)
            meta = [ac.get(k) for k in META]
            meta[-1] = int(bool(meta[-1]))
            flight = self.db.execute(
                "INSERT INTO air_flights (icao, reg, callsign, type, type_name, operator, helicopter, "
                "start_ts, end_ts, points, max_alt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (icao, *meta, ts, ts, alt)).lastrowid
        self.db.execute("INSERT OR REPLACE INTO air_points VALUES (?, ?, ?, ?, ?, ?)",
                        (icao, ts, round(lat, 5), round(lon, 5), alt, None if trk is None else round(trk)))
        if commit:
            self.db.commit()
        self.last[icao] = _Last(flight, ts, lat, lon, alt, trk)
        return flight

    def _resume(self, icao: str) -> _Last | None:
        """After a restart, carry on with the aircraft's latest flight if it ended recently."""
        row = self.db.execute("SELECT id, end_ts FROM air_flights WHERE icao = ? ORDER BY end_ts DESC LIMIT 1",
                              (icao,)).fetchone()
        if not row:
            return None
        p = self.db.execute("SELECT ts, lat, lon, alt, trk FROM air_points WHERE icao = ? AND ts = ?",
                            (icao, row[1])).fetchone()
        return _Last(row[0], *p) if p else None

    def _fill_meta(self, flight: int, ac: dict[str, Any]) -> None:
        # A callsign or type that arrives later in the flight fills a gap, never overwrites.
        if ac.get("callsign") or ac.get("type_name"):
            self.db.execute("UPDATE air_flights SET callsign = coalesce(callsign, ?), "
                            "type_name = coalesce(type_name, ?) WHERE id = ?",
                            (ac.get("callsign"), ac.get("type_name"), flight))

    def finish(self, flight: int) -> None:
        """Store the simplified line of a flight that has ended."""
        row = self.db.execute("SELECT icao, start_ts, end_ts FROM air_flights WHERE id = ?", (flight,)).fetchone()
        if row:
            line = encode(simplify(self._points(*row), LINE_TOLERANCE_M))
            self.db.execute("UPDATE air_flights SET line = ? WHERE id = ?", (line, flight))
            self.db.commit()

    def tick(self, now: float) -> None:
        """Forget aircraft not heard from in FLIGHT_GAP and finish their flights."""
        for icao, last in list(self.last.items()):
            if now - last.ts > FLIGHT_GAP:
                self.finish(last.flight)
                del self.last[icao]

    def flight_of(self, icao: str) -> int | None:
        last = self.last.get(icao.lower())
        return last.flight if last else None

    def prune(self, now: float | None = None) -> int:
        cutoff = int((now or time.time()) - KEEP_DAYS * 86400)
        icaos = [r[0] for r in self.db.execute("SELECT DISTINCT icao FROM air_flights WHERE start_ts < ?", (cutoff,))]
        removed = 0
        for icao in icaos:  # each delete walks the (icao, ts) key, never the whole table
            removed += self.db.execute("DELETE FROM air_points WHERE icao = ? AND ts < ?", (icao, cutoff)).rowcount
        self.db.execute("DELETE FROM air_flights WHERE end_ts < ?", (cutoff,))
        self.db.commit()
        return removed

    # --- reading ---------------------------------------------------------

    def _points(self, icao: str, start: int, end: int) -> list[tuple[float, float, int]]:
        return self.db.execute("SELECT lat, lon, ts FROM air_points WHERE icao = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                               (icao, start, end)).fetchall()

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict[str, Any]:
        out = {k: row[k] for k in ("id", "icao", *META, "start_ts", "end_ts", "points", "max_alt")}
        out["helicopter"] = bool(out["helicopter"])
        out["dist_km"] = round(row["dist_m"] / 1000, 1)
        return out

    def flights(self, since: int, until: int, icao: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        sql = "SELECT * FROM air_flights WHERE end_ts >= ? AND start_ts <= ?"
        args: list[Any] = [since, until]
        if icao:
            sql += " AND icao = ?"
            args.append(icao.lower())
        rows = self.db.execute(sql + " ORDER BY start_ts DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._summary(r) for r in rows]

    def flight(self, flight_id: int) -> dict[str, Any] | None:
        """A flight with all its points as [lat, lon, ts, alt_m]."""
        row = self.db.execute("SELECT * FROM air_flights WHERE id = ?", (flight_id,)).fetchone()
        if not row:
            return None
        points = self.db.execute(
            "SELECT lat, lon, ts, alt FROM air_points WHERE icao = ? AND ts BETWEEN ? AND ? ORDER BY ts",
            (row["icao"], row["start_ts"], row["end_ts"])).fetchall()
        return {**self._summary(row), "track": [list(p) for p in points]}

    def overview(self, since: int, until: int) -> Iterator[str]:
        """The JSON of every flight in a period as simplified lines, in pieces, so a year
        is never held whole: {"flights": [[id, icao, start, end, max_alt, km, line], ...],
        "aircraft": {icao: {...}}, "tolerance": m}. Newest flights first."""
        tolerance = overview_tolerance(until - since)
        aircraft: dict[str, dict[str, Any]] = {}
        yield f'{{"since":{since},"until":{until},"tolerance":{tolerance},"flights":['
        first = True
        for row in self._batches(since, until):
            (fid, icao, reg, callsign, type_, type_name, op, heli, start, end, max_alt, dist, line) = row
            points = decode(line) if line else self._points(icao, start, end)
            if not line:
                points = simplify(points, LINE_TOLERANCE_M)
            if tolerance > LINE_TOLERANCE_M:
                points = simplify(points, tolerance)
            if len(points) < 2:
                continue
            info = aircraft.setdefault(icao, {"reg": reg, "type": type_, "type_name": type_name, "operator": op,
                                              "helicopter": bool(heli), "flights": 0, "seconds": 0})
            info["flights"] += 1
            info["seconds"] += end - start
            info["callsign"] = info.get("callsign") or callsign
            item = [fid, icao, start, end, max_alt, round(dist / 1000, 1), encode(points)]
            yield ("" if first else ",") + json.dumps(item, separators=(",", ":"))
            first = False
        yield '],"aircraft":' + json.dumps(aircraft, separators=(",", ":")) + "}"

    def _batches(self, since: int, until: int, size: int = 200) -> Iterator[tuple]:
        """Flights newest first, read 200 at a time: no cursor stays open while the
        caller writes to the panel (and positions are stored meanwhile)."""
        before, seen = (until + 1, 0), 0
        while seen < MAX_FLIGHTS:
            rows = self.db.execute(
                "SELECT id, icao, reg, callsign, type, type_name, operator, helicopter, start_ts, end_ts, max_alt, "
                "dist_m, line FROM air_flights WHERE end_ts >= ? AND (start_ts, id) < (?, ?) "
                "ORDER BY start_ts DESC, id DESC LIMIT ?", (since, *before, size)).fetchall()
            yield from rows
            seen += len(rows)
            if len(rows) < size:
                return
            before = (rows[-1][8], rows[-1][0])

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM air_flights").fetchone()[0]
