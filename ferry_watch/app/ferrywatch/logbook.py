"""Trip logbook from ben's own boat: tracks exported from the Garmin plotter (GPX).

Every uploaded track point with a time goes into one table, one row per second, so the
same card can be uploaded again and again: only new points are added. Trips are cut from
that one long track where the boat lay still or the plotter was off (a gap in the log), so
a trip that was only half on an earlier card is whole after the next upload.

Garmin keeps time only in the active track; a saved track has its points but no times.
Those are kept as they come (one trip each, with distance but no date or speeds).

Depth and water temperature come from the track points' Garmin extensions (gpxtpx:depth,
gpxtpx:wtemp, gpxx:Depth, gpxx:Temperature) when the sounder was on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Iterable

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS logpoints (
    ts INTEGER PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    sog REAL,
    depth REAL,
    wtemp REAL
);
CREATE TABLE IF NOT EXISTS untimed (
    id TEXT PRIMARY KEY,
    name TEXT,
    added_ts INTEGER NOT NULL,
    points TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tripinfo (
    id TEXT PRIMARY KEY,
    name TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS hidden (
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL
);
"""

NM = 1852.0
GAP_S = 20 * 60          # no log for this long: the trip ended (plotter off, or logging only when moving)
STILL_S = 20 * 60        # lying within STILL_M of one place for this long: moored
STILL_M = 60.0
MOVING_KN = 1.0          # slower than this is drifting, not underway
MIN_TRIP_M = 300.0       # shorter trips are a boat moved along the jetty
MIN_TRIP_S = 120
MAX_KN = 60.0            # faster between two points is a GPS jump, dropped
TOP_SPEED_S = 30         # top speed over at least this long, so one jumpy fix doesn't count
MAX_DRAW_POINTS = 6000
MAX_UNTIMED = 500


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _float(text: str | None) -> float | None:
    try:
        v = float((text or "").strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _time(text: str | None) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))


def parse_gpx(f: IO[bytes]) -> dict[str, Any]:
    """Track points from a GPX file, read as a stream (a whole season's card is fine).

    Returns {"timed": [(ts, lat, lon, sog, depth, wtemp)], "untimed": [{"name", "points"}],
    "tracks": n}. Routes and waypoints are left out: they are plans, not trips."""
    timed: list[tuple] = []
    untimed: list[dict[str, Any]] = []
    tracks = 0
    trk_name: str | None = None
    trk_untimed: list[list[float]] = []
    in_trk = False
    for event, el in ET.iterparse(f, events=("start", "end")):
        tag = _local(el.tag)
        if event == "start":
            if tag == "trk":
                in_trk, trk_name, trk_untimed = True, None, []
            continue
        if tag == "name" and in_trk and trk_name is None:
            trk_name = (el.text or "").strip() or None
        elif tag == "trkpt":
            lat, lon = _float(el.get("lat")), _float(el.get("lon"))
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
                el.clear()
                continue
            ts = sog = depth = wtemp = None
            for child in el.iter():
                name = _local(child.tag)
                if name == "time":
                    ts = _time(child.text)
                elif name == "depth":
                    depth = _float(child.text)
                elif name in ("wtemp", "watertemp", "watertemperature"):
                    wtemp = _float(child.text)
                elif name == "temperature" and wtemp is None:
                    # gpxx:Temperature on a marine unit is the transducer's water temperature.
                    wtemp = _float(child.text)
                elif name == "speed":
                    s = _float(child.text)  # m/s
                    sog = None if s is None else s / NM * 3600
            if depth is not None and not (0 < depth < 500):
                depth = None
            if wtemp is not None and not (-3 < wtemp < 40):
                wtemp = None
            if ts is not None:
                timed.append((ts, round(lat, 6), round(lon, 6),
                              None if sog is None else round(sog, 2),
                              None if depth is None else round(depth, 2),
                              None if wtemp is None else round(wtemp, 2)))
            else:
                trk_untimed.append([round(lat, 6), round(lon, 6)])
            el.clear()
        elif tag == "trk":
            tracks += 1
            if len(trk_untimed) >= 2:
                untimed.append({"name": trk_name, "points": trk_untimed})
            in_trk = False
            el.clear()
        elif tag in ("wpt", "rte"):
            el.clear()
    return {"timed": timed, "untimed": untimed, "tracks": tracks}


def _clean(points: list[tuple]) -> list[tuple]:
    """Drop GPS jumps: a point the boat couldn't have reached from the last good one."""
    out: list[tuple] = []
    for p in points:
        if out:
            q = out[-1]
            dt = p[0] - q[0]
            if dt <= 0:
                continue
            if dist_m(q[1], q[2], p[1], p[2]) / dt * 3600 / NM > MAX_KN:
                continue
        out.append(p)
    return out


def split_trips(points: list[tuple]) -> list[list[tuple]]:
    """Cut one long track (sorted by time) into trips: at gaps in the log, and where the
    boat lay within STILL_M of one place for STILL_S or longer (those points left out)."""
    points = _clean(points)
    runs: list[list[tuple]] = []
    cur: list[tuple] = []
    for p in points:
        if cur and p[0] - cur[-1][0] >= GAP_S:
            runs.append(cur)
            cur = []
        cur.append(p)
    if cur:
        runs.append(cur)
    trips: list[list[tuple]] = []
    for run in runs:
        # Lying still: from an anchor point, every later point within STILL_M for STILL_S.
        i, start = 0, 0
        n = len(run)
        while i < n:
            j = i
            while j + 1 < n and dist_m(run[i][1], run[i][2], run[j + 1][1], run[j + 1][2]) <= STILL_M:
                j += 1
            if run[j][0] - run[i][0] >= STILL_S:
                if i > start:
                    trips.append(run[start:i + 1])
                start = j
                i = j + 1
            else:
                i += 1
        if n - start >= 2:
            trips.append(run[start:])
    return [t for t in trips if _length(t) >= MIN_TRIP_M and t[-1][0] - t[0][0] >= MIN_TRIP_S]


def _length(pts: Iterable) -> float:
    total, prev = 0.0, None
    for p in pts:
        if prev is not None:
            total += dist_m(prev[-2], prev[-1], p[-2], p[-1]) if len(p) == 2 else dist_m(prev[1], prev[2], p[1], p[2])
        prev = p
    return total


def _stat(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"min": round(min(values), 1), "max": round(max(values), 1), "avg": round(sum(values) / len(values), 1)}


def summarise(pts: list[tuple]) -> dict[str, Any]:
    """Distance, times and speeds of one trip."""
    dist = moving_s = 0.0
    for a, b in zip(pts, pts[1:]):
        d = dist_m(a[1], a[2], b[1], b[2])
        dt = b[0] - a[0]
        dist += d
        if dt > 0 and dt < GAP_S and d / dt * 3600 / NM >= MOVING_KN:
            moving_s += dt
    # Top speed: the fastest stretch of at least TOP_SPEED_S (two pointers along the track).
    top = 0.0
    j = 0
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + dist_m(a[1], a[2], b[1], b[2]))
    for i in range(len(pts)):
        while j < len(pts) and pts[j][0] - pts[i][0] < TOP_SPEED_S:
            j += 1
        if j >= len(pts):
            break
        dt = pts[j][0] - pts[i][0]
        if dt <= 4 * TOP_SPEED_S:
            top = max(top, (cum[j] - cum[i]) / dt * 3600 / NM)
    sogs = [p[3] for p in pts if p[3] is not None]
    if not top and sogs:
        top = max(sogs)
    depths = [p[4] for p in pts if p[4] is not None]
    temps = [p[5] for p in pts if p[5] is not None]
    return {
        "start": pts[0][0], "end": pts[-1][0],
        "duration_s": pts[-1][0] - pts[0][0],
        "moving_s": int(moving_s),
        "distance_nm": round(dist / NM, 2),
        "avg_kn": round(dist / NM / (moving_s / 3600), 1) if moving_s > 60 else None,
        "top_kn": round(top, 1) if top else None,
        "depth": _stat(depths), "wtemp": _stat(temps),
        "from": [pts[0][1], pts[0][2]], "to": [pts[-1][1], pts[-1][2]],
        "bbox": [min(p[2] for p in pts), min(p[1] for p in pts), max(p[2] for p in pts), max(p[1] for p in pts)],
        "points": len(pts),
    }


def thin(pts: list, limit: int = MAX_DRAW_POINTS) -> list:
    if len(pts) <= limit:
        return pts
    step = len(pts) / limit
    out = [pts[int(i * step)] for i in range(limit)]
    if out[-1] is not pts[-1]:
        out.append(pts[-1])
    return out


class Logbook:
    def __init__(self, path: Path | str, harbours: Any = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA cache_size=-1000")
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.harbours = harbours
        self._trips: list[list[tuple]] | None = None

    def close(self) -> None:
        self.db.close()

    # --- adding ------------------------------------------------------------------------

    def add_gpx(self, f: IO[bytes], filename: str = "") -> dict[str, Any]:
        """Read one GPX file into the logbook. Returns what was new in it."""
        before = {t["id"] for t in self.trips()}
        parsed = parse_gpx(f)
        hidden = self.db.execute("SELECT start_ts, end_ts FROM hidden").fetchall()
        rows = [p for p in parsed["timed"] if not any(a <= p[0] <= b for a, b in hidden)]
        with self.db:
            cur = self.db.executemany(
                "INSERT OR IGNORE INTO logpoints (ts, lat, lon, sog, depth, wtemp) VALUES (?, ?, ?, ?, ?, ?)", rows)
            new_points = cur.rowcount if cur.rowcount >= 0 else 0
            new_untimed = 0
            for tr in parsed["untimed"]:
                tid = "u" + hashlib.sha1(json.dumps(tr["points"]).encode()).hexdigest()[:12]
                count = self.db.execute("SELECT COUNT(*) FROM untimed").fetchone()[0]
                if count >= MAX_UNTIMED:
                    break
                c = self.db.execute("INSERT OR IGNORE INTO untimed (id, name, added_ts, points) VALUES (?, ?, ?, ?)",
                                    (tid, tr["name"], int(time.time()), json.dumps(tr["points"], separators=(",", ":"))))
                new_untimed += max(c.rowcount, 0)
        self._trips = None
        after = {t["id"] for t in self.trips()}
        return {
            "file": filename, "tracks": parsed["tracks"], "points": len(parsed["timed"]),
            "new_points": new_points, "new_trips": len(after - before),
            "untimed": len(parsed["untimed"]), "new_untimed": new_untimed,
            "depth": any(p[4] is not None for p in parsed["timed"]),
            "wtemp": any(p[5] is not None for p in parsed["timed"]),
        }

    # --- reading -----------------------------------------------------------------------

    def _timed_trips(self) -> list[list[tuple]]:
        if self._trips is None:
            pts = [tuple(r) for r in self.db.execute(
                "SELECT ts, lat, lon, sog, depth, wtemp FROM logpoints ORDER BY ts")]
            self._trips = split_trips(pts)
        return self._trips

    def _place(self, lat: float, lon: float) -> str | None:
        """The nearest harbour within about 2 km, by name."""
        if not self.harbours:
            return None
        try:
            dl = 0.02
            rows = self.harbours.in_view(lat - dl, lon - dl * 2, lat + dl, lon + dl * 2).get("harbours") or []
        except Exception:  # noqa: BLE001 - names are a nicety
            return None
        best, best_d = None, 2000.0
        for r in rows:
            if not r[3]:
                continue
            d = dist_m(lat, lon, r[1], r[2])
            if d < best_d:
                best, best_d = r[3], d
        return best

    def _info(self) -> dict[str, dict[str, Any]]:
        return {r[0]: {"name": r[1], "note": r[2]} for r in self.db.execute("SELECT id, name, note FROM tripinfo")}

    def trips(self) -> list[dict[str, Any]]:
        info = self._info()
        out = []
        for pts in self._timed_trips():
            s = summarise(pts)
            tid = f"t{pts[0][0]}"
            s.update(id=tid, timed=True, **info.get(tid, {"name": None, "note": None}))
            out.append(s)
        for tid, name, added, raw in self.db.execute("SELECT id, name, added_ts, points FROM untimed"):
            pts = json.loads(raw)
            i = info.get(tid, {})
            out.append({
                "id": tid, "timed": False, "start": None, "added": added,
                "name": i.get("name") or name, "note": i.get("note"),
                "distance_nm": round(_length(pts) / NM, 2), "points": len(pts),
                "from": pts[0], "to": pts[-1],
                "bbox": [min(p[1] for p in pts), min(p[0] for p in pts), max(p[1] for p in pts), max(p[0] for p in pts)],
            })
        return out

    def listing(self) -> dict[str, Any]:
        trips = self.trips()
        for t in trips:
            t["from_name"] = self._place(*t["from"])
            t["to_name"] = self._place(*t["to"])
        trips.sort(key=lambda t: t["start"] or 0, reverse=True)
        timed = [t for t in trips if t["timed"]]
        years: dict[str, dict[str, float]] = {}
        for t in timed:
            y = str(datetime.fromtimestamp(t["start"], timezone.utc).year)
            row = years.setdefault(y, {"trips": 0, "distance_nm": 0.0, "moving_s": 0})
            row["trips"] += 1
            row["distance_nm"] = round(row["distance_nm"] + t["distance_nm"], 1)
            row["moving_s"] += t["moving_s"]
        return {"trips": trips, "years": years}

    def trip(self, tid: str) -> dict[str, Any] | None:
        if tid.startswith("u"):
            row = self.db.execute("SELECT points FROM untimed WHERE id = ?", (tid,)).fetchone()
            if not row:
                return None
            return {"id": tid, "timed": False, "points": thin(json.loads(row[0]))}
        for pts in self._timed_trips():
            if f"t{pts[0][0]}" == tid:
                # [lat, lon, ts, knots, depth, wtemp]; knots from the next point when Garmin gave none.
                out = []
                for k, p in enumerate(pts):
                    sog = p[3]
                    if sog is None:
                        q = pts[k + 1] if k + 1 < len(pts) else pts[k - 1] if k else None
                        if q and q[0] != p[0]:
                            sog = round(dist_m(p[1], p[2], q[1], q[2]) / abs(q[0] - p[0]) * 3600 / NM, 1)
                    out.append([p[1], p[2], p[0], sog, p[4], p[5]])
                return {"id": tid, "timed": True, "points": thin(out)}
        return None

    # --- changing ----------------------------------------------------------------------

    def set_info(self, tid: str, name: str | None, note: str | None) -> bool:
        if not any(t["id"] == tid for t in self.trips()):
            return False
        name = (name or "").strip()[:80] or None
        note = (note or "").strip()[:2000] or None
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO tripinfo (id, name, note) VALUES (?, ?, ?)", (tid, name, note))
        return True

    def delete(self, tid: str) -> bool:
        """Remove a trip. Its stretch of the log stays out when the same card comes again."""
        if tid.startswith("u"):
            with self.db:
                c = self.db.execute("DELETE FROM untimed WHERE id = ?", (tid,))
                self.db.execute("DELETE FROM tripinfo WHERE id = ?", (tid,))
            return c.rowcount > 0
        for pts in self._timed_trips():
            if f"t{pts[0][0]}" == tid:
                a, b = pts[0][0], pts[-1][0]
                with self.db:
                    self.db.execute("DELETE FROM logpoints WHERE ts BETWEEN ? AND ?", (a, b))
                    self.db.execute("INSERT INTO hidden (start_ts, end_ts) VALUES (?, ?)", (a, b))
                    self.db.execute("DELETE FROM tripinfo WHERE id = ?", (tid,))
                self._trips = None
                return True
        return False
