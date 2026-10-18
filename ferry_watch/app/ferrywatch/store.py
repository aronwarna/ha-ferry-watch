"""SQLite storage for ships, positions, voyages and vessel metadata.

Volumes are tiny (a few rows a minute), so plain synchronous sqlite3 on the
event loop is fine.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS ships (
    mmsi INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    color TEXT NOT NULL,
    wiki TEXT,
    auto_resolve INTEGER NOT NULL DEFAULT 0,
    added_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    mmsi INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    sog REAL,
    cog REAL,
    heading INTEGER
);
CREATE INDEX IF NOT EXISTS positions_mmsi_ts ON positions (mmsi, ts);
CREATE TABLE IF NOT EXISTS voyages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi INTEGER NOT NULL,
    start_ts INTEGER NOT NULL,
    from_port TEXT
);
CREATE INDEX IF NOT EXISTS voyages_mmsi_start ON voyages (mmsi, start_ts);
CREATE TABLE IF NOT EXISTS meta (
    mmsi INTEGER PRIMARY KEY,
    name TEXT,
    destination TEXT,
    imo INTEGER,
    call_sign TEXT,
    ship_type INTEGER,
    updated_ts INTEGER
);
CREATE TABLE IF NOT EXISTS zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    shape TEXT NOT NULL,
    points TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS passages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    route TEXT NOT NULL,
    direction TEXT NOT NULL,
    zone_id INTEGER,
    lat REAL,
    lon REAL,
    sog REAL,
    notified INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE INDEX IF NOT EXISTS passages_mmsi_route_ts ON passages (mmsi, route, ts);
CREATE INDEX IF NOT EXISTS passages_ts ON passages (ts);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Position:
    mmsi: int
    ts: int
    lat: float
    lon: float
    sog: float | None = None
    cog: float | None = None
    heading: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mmsi": self.mmsi,
            "ts": self.ts,
            "lat": self.lat,
            "lon": self.lon,
            "sog": self.sog,
            "cog": self.cog,
            "heading": self.heading,
        }


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- ships -----------------------------------------------------------

    def ships(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM ships ORDER BY added_ts, rowid").fetchall()
        return [dict(r) for r in rows]

    def ship(self, mmsi: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM ships WHERE mmsi = ?", (mmsi,)).fetchone()
        return dict(row) if row else None

    def add_ship(self, mmsi: int, name: str, color: str, wiki: str | None = None,
                 auto_resolve: bool = False) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO ships (mmsi, name, color, wiki, auto_resolve, added_ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mmsi, name, color, wiki, int(auto_resolve), int(time.time())),
        )
        self.db.commit()

    def remove_ship(self, mmsi: int) -> None:
        ship = self.ship(mmsi)
        if ship:
            # Keep the name and colour so its passages still show up in the stats.
            former = self.kv_get("former_ships", {})
            former[str(mmsi)] = {"name": ship["name"], "color": ship["color"]}
            self.kv_set("former_ships", former)
        self.db.execute("DELETE FROM ships WHERE mmsi = ?", (mmsi,))
        self.db.commit()

    # Ships that never send phone alerts. The four ferries alert; ships added later don't
    # until their Alerts switch is turned on.
    def alerts_on(self, mmsi: int) -> bool:
        return mmsi not in self.kv_get("quiet_ships", [])

    def set_alerts(self, mmsi: int, on: bool) -> None:
        quiet = set(self.kv_get("quiet_ships", []))
        if on:
            quiet.discard(mmsi)
        else:
            quiet.add(mmsi)
        self.kv_set("quiet_ships", sorted(quiet))

    def change_mmsi(self, old: int, new: int) -> None:
        """Move a ship and its history to a new MMSI (after a re-flag)."""
        with self.db:
            for table in ("ships", "positions", "voyages", "passages"):
                self.db.execute(f"UPDATE {table} SET mmsi = ? WHERE mmsi = ?", (new, old))
            self.db.execute("UPDATE ships SET auto_resolve = 0 WHERE mmsi = ?", (new,))
        if not self.alerts_on(old):
            self.set_alerts(old, True)
            self.set_alerts(new, False)

    # --- positions -------------------------------------------------------

    def insert_position(self, p: Position) -> None:
        self.db.execute(
            "INSERT INTO positions (mmsi, ts, lat, lon, sog, cog, heading) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (p.mmsi, p.ts, p.lat, p.lon, p.sog, p.cog, p.heading),
        )
        self.db.commit()

    def last_position(self, mmsi: int) -> Position | None:
        row = self.db.execute(
            "SELECT * FROM positions WHERE mmsi = ? ORDER BY ts DESC LIMIT 1", (mmsi,)
        ).fetchone()
        return Position(**dict(row)) if row else None

    def first_position_ts(self, mmsi: int) -> int | None:
        row = self.db.execute("SELECT MIN(ts) FROM positions WHERE mmsi = ?", (mmsi,)).fetchone()
        return row[0] if row else None

    def track(self, mmsi: int, since_ts: int, until_ts: int | None = None,
              speed: bool = False) -> list[list[float]]:
        """Positions as compact [lat, lon, ts] triples, oldest first; with speed, [lat, lon, ts, sog]."""
        if until_ts is None:
            until_ts = 2**62
        rows = self.db.execute(
            "SELECT lat, lon, ts, sog FROM positions WHERE mmsi = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (mmsi, since_ts, until_ts),
        ).fetchall()
        return [list(r) if speed else [r[0], r[1], r[2]] for r in rows]

    def stopped_since(self, mmsi: int, stop_speed: float) -> int | None:
        """Start of the stop the ship is currently in, or None if it is moving."""
        last_moving = self.db.execute(
            "SELECT MAX(ts) FROM positions WHERE mmsi = ? AND sog >= ?", (mmsi, stop_speed)
        ).fetchone()[0]
        row = self.db.execute(
            "SELECT MIN(ts) FROM positions WHERE mmsi = ? AND ts > ?", (mmsi, last_moving or 0)
        ).fetchone()
        return row[0] if row else None

    def prune(self, older_than_ts: int) -> int:
        cur = self.db.execute("DELETE FROM positions WHERE ts < ?", (older_than_ts,))
        self.db.commit()
        return cur.rowcount

    def prune_untracked(self, older_than_ts: int) -> int:
        """Remove old history of ships that aren't configured (fleet ships): they only
        need their current voyage."""
        untracked = "mmsi NOT IN (SELECT mmsi FROM ships)"
        cur = self.db.execute(f"DELETE FROM positions WHERE ts < ? AND {untracked}", (older_than_ts,))
        self.db.execute(f"DELETE FROM voyages WHERE start_ts < ? AND {untracked}", (older_than_ts,))
        self.db.commit()
        return cur.rowcount

    def position_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]

    # --- voyages ---------------------------------------------------------

    def start_voyage(self, mmsi: int, start_ts: int, from_port: str | None) -> None:
        self.db.execute(
            "INSERT INTO voyages (mmsi, start_ts, from_port) VALUES (?, ?, ?)",
            (mmsi, start_ts, from_port),
        )
        self.db.commit()

    def current_voyage(self, mmsi: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM voyages WHERE mmsi = ? ORDER BY start_ts DESC LIMIT 1", (mmsi,)
        ).fetchone()
        return dict(row) if row else None

    # --- metadata --------------------------------------------------------

    def upsert_meta(self, mmsi: int, meta: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO meta (mmsi, name, destination, imo, call_sign, ship_type, updated_ts) "
            "VALUES (:mmsi, :name, :destination, :imo, :call_sign, :ship_type, :updated_ts) "
            "ON CONFLICT(mmsi) DO UPDATE SET name=excluded.name, destination=excluded.destination, "
            "imo=excluded.imo, call_sign=excluded.call_sign, ship_type=excluded.ship_type, "
            "updated_ts=excluded.updated_ts",
            {
                "mmsi": mmsi,
                "name": meta.get("name"),
                "destination": meta.get("destination"),
                "imo": meta.get("imo"),
                "call_sign": meta.get("call_sign"),
                "ship_type": meta.get("ship_type"),
                "updated_ts": meta.get("updated_ts") or int(time.time()),
            },
        )
        self.db.commit()

    def meta(self, mmsi: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM meta WHERE mmsi = ?", (mmsi,)).fetchone()
        return dict(row) if row else None

    # --- zones -----------------------------------------------------------

    @staticmethod
    def _zone(row: sqlite3.Row) -> dict[str, Any]:
        zone = dict(row)
        zone["points"] = json.loads(zone["points"])
        return zone

    def zones(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM zones ORDER BY id").fetchall()
        return [self._zone(r) for r in rows]

    def zone(self, zone_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM zones WHERE id = ?", (zone_id,)).fetchone()
        return self._zone(row) if row else None

    def add_zone(self, name: str, role: str, shape: str, points: list[list[float]]) -> int:
        cur = self.db.execute(
            "INSERT INTO zones (name, role, shape, points, created_ts) VALUES (?, ?, ?, ?, ?)",
            (name, role, shape, json.dumps(points), int(time.time())),
        )
        self.db.commit()
        return cur.lastrowid

    def update_zone(self, zone_id: int, name: str, points: list[list[float]]) -> None:
        self.db.execute("UPDATE zones SET name = ?, points = ? WHERE id = ?",
                        (name, json.dumps(points), zone_id))
        self.db.commit()

    def delete_zone(self, zone_id: int) -> None:
        self.db.execute("DELETE FROM zones WHERE id = ?", (zone_id,))
        self.db.commit()

    # --- passages --------------------------------------------------------

    def add_passage(self, p: dict[str, Any]) -> int:
        cur = self.db.execute(
            "INSERT INTO passages (mmsi, ts, route, direction, zone_id, lat, lon, sog) "
            "VALUES (:mmsi, :ts, :route, :direction, :zone_id, :lat, :lon, :sog)",
            p,
        )
        self.db.commit()
        return cur.lastrowid

    def set_passage_result(self, passage_id: int, notified: bool, note: str) -> None:
        self.db.execute("UPDATE passages SET notified = ?, note = ? WHERE id = ?",
                        (int(notified), note, passage_id))
        self.db.commit()

    def last_passage(self, mmsi: int, route: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM passages WHERE mmsi = ? AND route = ? ORDER BY ts DESC LIMIT 1", (mmsi, route)
        ).fetchone()
        return dict(row) if row else None

    def passages(self, limit: int = 50, route: str | None = None,
                 direction: str | None = None, before_ts: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM passages WHERE 1=1", []
        if route:
            sql += " AND route = ?"
            args.append(route)
        if direction:
            sql += " AND direction = ?"
            args.append(direction)
        if before_ts is not None:
            sql += " AND ts < ?"
            args.append(before_ts)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def passage_rows(self) -> list[tuple[int, int, str, str]]:
        """Every passage as (ts, mmsi, route, direction), oldest first, for the stats."""
        return [tuple(r) for r in self.db.execute(
            "SELECT ts, mmsi, route, direction FROM passages ORDER BY ts").fetchall()]

    # --- key/value -------------------------------------------------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.db.commit()
