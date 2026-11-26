"""When ships are due at, and leave from, their ports (the ship card's "Port times").

Two sources:

* Digitraffic's port calls (Portnet): what the ship's agent has announced for Finnish
  ports (Helsinki, Mariehamn, Långnäs, Turku): expected arrival and departure, and the
  actual ones once they happen. Fetched for those ports every 10 minutes, only calls
  changed since the last fetch, kept for a few days on disk.
* The ship's own history: every time a ship stops in one of the known ports, a visit is
  written down (arrived, left). From those come the usual crossing time between two ports
  and the usual stay, which give times for Tallinn and Stockholm (Portnet has no foreign
  ports), and the usual next port.

The crew's own ETA from AIS is used for its destination when no port call covers it.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from .config import USER_AGENT
from .geo import PORTS, distance_m

_LOGGER = logging.getLogger(__name__)

URL = "https://meri.digitraffic.fi/api/port-call/v1/port-calls"
HEADERS = {"Digitraffic-User": "ha-ferry-watch", "User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
LOCODES = {
    "FIHEL": "Helsinki", "FIMHQ": "Mariehamn", "FILAN": "Långnäs", "FITKU": "Turku",
    "EETLL": "Tallinn", "SESTO": "Stockholm", "SEKPS": "Kapellskär", "FIHKO": "Hanko",
    "SENYN": "Nynäshamn", "DETRV": "Travemünde", "DEROS": "Rostock", "PLGDY": "Gdynia",
    "RULED": "St Petersburg", "FIVAA": "Vaasa", "SEUME": "Umeå", "SEGRH": "Grisslehamn",
    "FIKOK": "Kokkola", "FIKTK": "Kotka", "FIHMN": "Hamina", "DEKEL": "Kiel",
}
WATCHED = ("FIHEL", "FIMHQ", "FILAN", "FITKU")  # Finnish ports the ferries call at
FETCH_EVERY = 600  # s
FIRST_LOOKBACK = 3 * 86400  # s of changed calls asked for after a restart with nothing on disk
KEEP = 3 * 86400  # a call is dropped this long after its last time
SAVE_REV = 1

# Visits from the ship's own positions.
STOP_KNOTS = 1.0
MOVE_KNOTS = 2.0
MIN_STOP = 4 * 60  # s; a shorter stop isn't a port call (Mariehamn stops are ~15 min)
PORT_RADIUS_M = 5_000
VISIT_KEEP = 90 * 86400
USUAL_OF = 12  # the last n legs or stays make the usual time
USUAL_FROM_OTHERS = 3  # other ships' legs count when this ship has fewer
FUTURE_HORIZON = 2 * 86400
AIS_TRUST = 6 * 3600  # an AIS ETA further than this from the usual time is ignored

SCHEMA = """
CREATE TABLE IF NOT EXISTS port_visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi INTEGER NOT NULL,
    port TEXT NOT NULL,
    arrive_ts INTEGER NOT NULL,
    leave_ts INTEGER
);
CREATE INDEX IF NOT EXISTS port_visits_mmsi ON port_visits (mmsi, arrive_ts);
"""

Fetch = Callable[[dict[str, Any]], Awaitable[Any]]


def _ts(value: Any) -> int | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def place(code: Any) -> str | None:
    if not isinstance(code, str) or not code.strip():
        return None
    code = code.strip().upper()
    return LOCODES.get(code) or code


def parse_calls(data: Any) -> list[dict[str, Any]]:
    """Digitraffic's port-call answer as small dicts: one per call, its times taken from
    the port area (a call usually has one; the first one with times wins)."""
    calls = data.get("portCalls") if isinstance(data, dict) else data
    out = []
    for c in calls or []:
        if not isinstance(c, dict):
            continue
        try:
            mmsi = int(c.get("mmsi") or 0)
        except (TypeError, ValueError):
            mmsi = 0
        if not mmsi or not c.get("portCallId"):
            continue
        times: dict[str, Any] = {}
        for area in c.get("portAreaDetails") or []:
            if not isinstance(area, dict):
                continue
            got = {k: _ts(area.get(k)) for k in ("eta", "etd", "ata", "atd")}
            if any(got.values()):
                times = {**got, "berth": area.get("berthName") or area.get("portAreaName") or None}
                break
        if not times:
            continue
        out.append({
            "id": c["portCallId"], "mmsi": mmsi, "port": place(c.get("portToVisit")),
            "prev": place(c.get("prevPort")), "next": place(c.get("nextPort")),
            "updated": _ts(c.get("portCallTimestamp")), **times,
        })
    return out


def _last_time(call: dict[str, Any]) -> int:
    return max(call.get(k) or 0 for k in ("eta", "etd", "ata", "atd"))


def port_at(lat: float, lon: float) -> str | None:
    for name, plat, plon in PORTS:
        if distance_m(lat, lon, plat, plon) <= PORT_RADIUS_M:
            return name
    return None


def _median(values: list[float]) -> int | None:
    return int(statistics.median(values)) if values else None


class VisitLog:
    """Writes down port visits from each position a tracker hears (or from stored tracks)."""

    def __init__(self, db):
        self.db = db
        self.db.executescript(SCHEMA)
        self._open: dict[int, dict[str, Any]] = {}  # mmsi -> {port, since, id}
        for row in db.execute("SELECT id, mmsi, port, arrive_ts FROM port_visits WHERE leave_ts IS NULL"):
            self._open[row[1]] = {"port": row[2], "since": row[3], "id": row[0]}
        self._stop: dict[int, tuple[str, int]] = {}  # stopped in a port, not yet long enough
        self._cache: dict[int, tuple[float, list]] = {}

    def see(self, mmsi: int, ts: int, lat: float, lon: float, sog: float | None) -> None:
        if sog is None:
            return
        open_ = self._open.get(mmsi)
        if sog < STOP_KNOTS:
            if open_:
                return
            port = port_at(lat, lon)
            if not port:
                return
            pending = self._stop.get(mmsi)
            if not pending or pending[0] != port:
                self._stop[mmsi] = (port, ts)
            elif ts - pending[1] >= MIN_STOP:
                cur = self.db.execute("INSERT INTO port_visits (mmsi, port, arrive_ts) VALUES (?, ?, ?)",
                                      (mmsi, port, pending[1]))
                self.db.commit()
                self._open[mmsi] = {"port": port, "since": pending[1], "id": cur.lastrowid}
                self._stop.pop(mmsi, None)
                self._cache.pop(mmsi, None)
        elif sog >= MOVE_KNOTS:
            self._stop.pop(mmsi, None)
            if open_:
                self.db.execute("UPDATE port_visits SET leave_ts = ? WHERE id = ?", (ts, open_["id"]))
                self.db.commit()
                del self._open[mmsi]
                self._cache.pop(mmsi, None)

    def backfill(self, store, mmsis: list[int], since: int) -> int:
        """Visits from the tracks already stored (once, for the history before 0.41.0)."""
        before = self.db.execute("SELECT COUNT(*) FROM port_visits").fetchone()[0]
        for mmsi in mmsis:
            if self.db.execute("SELECT 1 FROM port_visits WHERE mmsi = ? LIMIT 1", (mmsi,)).fetchone():
                continue
            for lat, lon, ts, sog in store.track(mmsi, since, speed=True):
                self.see(mmsi, int(ts), lat, lon, sog)
        return self.db.execute("SELECT COUNT(*) FROM port_visits").fetchone()[0] - before

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.db.execute("DELETE FROM port_visits WHERE arrive_ts < ?", (int(now - VISIT_KEEP),))
        self.db.commit()
        self._cache.clear()

    def visits(self, mmsi: int) -> list[tuple[str, int, int | None]]:
        """(port, arrived, left) oldest first, cached for a few minutes."""
        hit = self._cache.get(mmsi)
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
        rows = [tuple(r) for r in self.db.execute(
            "SELECT port, arrive_ts, leave_ts FROM port_visits WHERE mmsi = ? ORDER BY arrive_ts", (mmsi,))]
        self._cache[mmsi] = (time.monotonic(), rows)
        return rows

    def legs(self, mmsi: int | None = None) -> list[tuple[str, str, int, int]]:
        """(from, to, left, arrived) for back-to-back visits of one ship, or of every ship."""
        if mmsi is not None:
            rows = [(mmsi, *v) for v in self.visits(mmsi)]
        else:
            rows = [tuple(r) for r in self.db.execute(
                "SELECT mmsi, port, arrive_ts, leave_ts FROM port_visits ORDER BY mmsi, arrive_ts")]
        out = []
        for a, b in zip(rows, rows[1:]):
            if a[0] == b[0] and a[3] and a[1] != b[1] and 0 < b[2] - a[3] < 2 * 86400:
                out.append((a[1], b[1], a[3], b[2]))
        return out


class PortTimes:
    def __init__(self, session: aiohttp.ClientSession | None, db, path: Path,
                 fetch: Fetch | None = None):
        self.session = session
        self.path = path
        self.visits = VisitLog(db)
        self._fetch = fetch or self._get
        self.calls: dict[Any, dict[str, Any]] = {}
        self.fetched: float | None = None
        self.error: str | None = None
        self._load()

    # --- Digitraffic ----------------------------------------------------------

    def _load(self) -> None:
        try:
            saved = json.loads(gzip.decompress(self.path.read_bytes()))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            _LOGGER.warning("Could not read the saved port calls: %s", err)
            return
        if saved.get("rev") != SAVE_REV:
            return
        self.calls = {c["id"]: c for c in saved.get("calls", [])}
        self.fetched = saved.get("fetched")

    def _save(self) -> None:
        data = {"rev": SAVE_REV, "fetched": self.fetched, "calls": list(self.calls.values())}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode()))
        tmp.replace(self.path)

    async def _get(self, params: dict[str, Any]) -> Any:
        async with self.session.get(URL, params=params, headers=HEADERS,
                                    timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def refresh(self) -> None:
        now = time.time()
        since = max(self.fetched - 600, now - FIRST_LOOKBACK) if self.fetched else now - FIRST_LOOKBACK
        stamp = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        got = 0
        for code in WATCHED:
            data = await self._fetch({"locode": code, "from": stamp})
            for call in parse_calls(data):
                self.calls[call["id"]] = call
                got += 1
        for key in [k for k, c in self.calls.items() if _last_time(c) < now - KEEP]:
            del self.calls[key]
        self.fetched = now
        self.error = None
        await asyncio.to_thread(self._save)
        _LOGGER.debug("Port calls: %s changed, %s kept", got, len(self.calls))

    async def run(self) -> None:
        wait = 0.0
        while True:
            await asyncio.sleep(wait)
            try:
                await self.refresh()
                wait = FETCH_EVERY
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as err:
                self.error = str(err) or type(err).__name__
                _LOGGER.warning("Could not fetch port calls from Digitraffic: %s", self.error)
                wait = min(max(2 * wait, FETCH_EVERY), 6 * 3600)

    def status(self) -> dict[str, Any]:
        return {"fetched": self.fetched, "error": self.error, "calls": len(self.calls)}

    # --- for the ship card -----------------------------------------------------

    def calls_for(self, mmsi: int) -> list[dict[str, Any]]:
        return sorted((c for c in self.calls.values() if c["mmsi"] == mmsi),
                      key=lambda c: c.get("ata") or c.get("eta") or c.get("atd") or c.get("etd") or 0)

    def usual_leg(self, mmsi: int, a: str, b: str) -> int | None:
        own = [arr - left for f, t, left, arr in self.visits.legs(mmsi) if f == a and t == b][-USUAL_OF:]
        if len(own) < USUAL_FROM_OTHERS:
            own += [arr - left for f, t, left, arr in self.visits.legs() if f == a and t == b][-USUAL_OF:]
        return _median(own)

    def usual_stay(self, mmsi: int, port: str) -> int | None:
        stays = [left - arr for p, arr, left in self.visits.visits(mmsi) if p == port and left][-USUAL_OF:]
        return _median(stays) if stays else None

    def usual_next(self, mmsi: int, port: str, came_from: str | None = None) -> str | None:
        """Where this ship usually goes from port (its latest legs count most). Coming from
        somewhere, the legs that came the same way count first: from Mariehamn a ship from
        Helsinki goes on to Stockholm, not back."""
        legs = self.visits.legs(mmsi)
        pairs = [(a[0], b[1]) for a, b in zip(legs, legs[1:]) if a[1] == b[0] == port and a[3] <= b[2]]
        ahead = [t for f, t in pairs if f == came_from][-USUAL_OF:] if came_from else []
        if not ahead:
            ahead = [t for f, t, _, _ in legs if f == port][-USUAL_OF:]
        if not ahead:
            return None
        weights = Counter()
        for i, t in enumerate(ahead):
            weights[t] += 1 + i / len(ahead)
        return weights.most_common(1)[0][0]

    def _call_at(self, calls: list[dict[str, Any]], port: str, after: int, before: int) -> dict[str, Any] | None:
        for c in calls:
            t = c.get("ata") or c.get("eta")
            if c["port"] == port and t and after - 3600 <= t <= before:
                return c
        return None

    def summary(self, mmsi: int, ship: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        """Port times for the card: up to three rows (the port it left or is in, the next
        port and the one after), each {port, arrive, leave} with {ts, actual, src}."""
        now = int(time.time() if now is None else now)
        calls = self.calls_for(mmsi)
        visits = self.visits.visits(mmsi)
        rows: list[dict[str, Any]] = []

        def mark(ts, actual, src):
            return {"ts": int(ts), "actual": actual, "src": src} if ts else None

        # Where it is or was last.
        here = None
        last = visits[-1] if visits else None
        pos = ship.get("position") or {}
        if last and not last[2]:
            here = {"port": last[0], "arrive": mark(last[1], True, "ais"), "leave": None, "at": True}
        elif last and last[2] and now - last[2] < 2 * 86400:
            here = {"port": last[0], "arrive": mark(last[1], True, "ais"), "leave": mark(last[2], True, "ais")}
        elif ship.get("in_port") and ship.get("port"):
            here = {"port": ship["port"], "arrive": mark(ship.get("stopped_since"), True, "ais"),
                    "leave": None, "at": True}
        elif ship.get("voyage_from") and ship.get("voyage_start"):
            here = {"port": ship["voyage_from"], "arrive": None,
                    "leave": mark(ship["voyage_start"], True, "ais")}
        if here:
            arr = (here.get("arrive") or {}).get("ts") or (here.get("leave") or {}).get("ts") or now
            c = self._call_at(calls, here["port"], arr - 6 * 3600, arr + 6 * 3600)
            if c:
                here["call"] = c
                if c.get("berth"):
                    here["berth"] = c["berth"]
                if here.get("at") and not here["leave"]:
                    if c.get("atd"):
                        here["leave"] = mark(c["atd"], True, "portnet")
                    elif c.get("etd") and c["etd"] > now - 3600:
                        here["leave"] = mark(c["etd"], False, "portnet")
            if here.get("at") and not here["leave"] and here.get("arrive"):
                stay = self.usual_stay(mmsi, here["port"])
                if stay and here["arrive"]["ts"] + stay > now - 1800:
                    here["leave"] = mark(here["arrive"]["ts"] + stay, False, "usual")
            rows.append(here)

        # Next ports: an announced call first, then the AIS destination, then the usual one.
        prev = rows[-1] if rows else None
        came_from = None
        if prev and visits and len(visits) >= 2 and visits[-1][0] == prev["port"]:
            came_from = visits[-2][0]
        dest_code = (ship.get("destination") or "").replace(" ", "").upper()
        dest = None if "<>" in dest_code else place(dest_code.split(">")[-1].strip("-")) if dest_code else None
        for _ in range(2):
            if not prev:
                break
            left = (prev.get("leave") or {}).get("ts")
            pcall = prev.get("call")
            nxt = (pcall or {}).get("next")
            if not nxt and len(rows) == 1 and dest and dest != prev["port"]:
                nxt = dest
            upcoming = [c for c in calls if c["port"] != prev["port"] and not c.get("ata")
                        and (c.get("eta") or 0) > (left or now) - 3600 and (c.get("eta") or 0) < now + FUTURE_HORIZON]
            if not nxt and upcoming and (not upcoming[0].get("prev") or upcoming[0]["prev"] == prev["port"]):
                nxt = upcoming[0]["port"]
            if not nxt:
                nxt = self.usual_next(mmsi, prev["port"], came_from)
            if not nxt or nxt == prev["port"]:
                break
            row: dict[str, Any] = {"port": nxt, "arrive": None, "leave": None}
            usual = self.usual_leg(mmsi, prev["port"], nxt)
            c = self._call_at(calls, nxt, (left or now) - 3600, (left or now) + FUTURE_HORIZON)
            if c:
                row["call"] = c
                if c.get("berth"):
                    row["berth"] = c["berth"]
                if c.get("ata"):
                    row["arrive"] = mark(c["ata"], True, "portnet")
                elif c.get("eta"):
                    row["arrive"] = mark(c["eta"], False, "portnet")
                if c.get("atd"):
                    row["leave"] = mark(c["atd"], True, "portnet")
                elif c.get("etd"):
                    row["leave"] = mark(c["etd"], False, "portnet")
            if not row["arrive"]:
                guess = left + usual if left and usual else None
                ais = ship.get("eta_ais")
                if ais and nxt == dest and (not guess or abs(ais - guess) < AIS_TRUST):
                    row["arrive"] = mark(ais, False, "ais_eta")
                elif guess:
                    row["arrive"] = mark(guess, False, "usual")
            if usual:
                row["usual_trip"] = usual
            if not row["leave"] and row["arrive"]:
                stay = self.usual_stay(mmsi, nxt)
                if stay:
                    row["leave"] = mark(row["arrive"]["ts"] + stay, False, "usual")
            if not row["arrive"] and not row["leave"]:
                rows.append(row)
                break
            rows.append(row)
            came_from, prev = prev["port"], row
        if not rows:
            # A ship with no history here (another ship's popup): its announced calls alone.
            for c in calls:
                if _last_time(c) < now - 12 * 3600 or len(rows) == 2:
                    continue
                rows.append({
                    "port": c["port"], "berth": c.get("berth"),
                    "arrive": mark(c.get("ata"), True, "portnet") or mark(c.get("eta"), False, "portnet"),
                    "leave": mark(c.get("atd"), True, "portnet") or mark(c.get("etd"), False, "portnet"),
                })
            if rows and len(rows) == 1 and (nxt := calls[-1].get("next")) and nxt != rows[0]["port"]:
                rows.append({"port": nxt, "arrive": None, "leave": None})
        for r in rows:
            r.pop("call", None)
            r.pop("at", None)
        return {"rows": rows, "error": self.error, "fetched": self.fetched}
