"""Passage statistics for the panel and a CSV export.

The panel does the counting per month and time of day, so months and clock
times follow the viewer's time zone. There are only a few passages a day, so
sending them all stays small.
"""

from __future__ import annotations

import csv
import io
import time
from typing import Any

from .passages import DIRECTION_LABELS
from .store import Store


def ship_names(store: Store) -> dict[int, dict[str, Any]]:
    """Name and colour for every MMSI that may appear in passages, removed ships included."""
    ships = {int(m): {**info, "current": False} for m, info in store.kv_get("former_ships", {}).items()}
    for ship in store.ships():
        ships[ship["mmsi"]] = {"name": ship["name"], "color": ship["color"], "current": True}
    return ships


def payload(store: Store) -> dict[str, Any]:
    rows = store.passage_rows()
    names = ship_names(store)
    seen = {row[1] for row in rows}
    ships = [
        {"mmsi": mmsi, **info}
        for mmsi, info in names.items()
        if info["current"] or mmsi in seen
    ]
    for mmsi in seen - names.keys():
        ships.append({"mmsi": mmsi, "name": f"MMSI {mmsi}", "color": "#64748b", "current": False})
    # [ts, mmsi, route, direction, wind m/s, wind from °, ice state]
    return {"ships": ships, "passages": [list(r) for r in rows]}


def to_csv(store: Store) -> str:
    names = ship_names(store)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["time", "ship", "mmsi", "route", "direction", "speed_kn", "alert", "wind_ms", "wind_dir",
                     "gust_ms", "water_c", "visibility_km", "ice", "ice_cm", "ice_concentration"])

    def num(value: Any, digits: int = 1) -> str:
        return "" if value is None else f"{value:.{digits}f}"

    for row in store.passages(limit=1_000_000):
        writer.writerow([
            time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ts"])),
            names.get(row["mmsi"], {}).get("name", f"MMSI {row['mmsi']}"),
            row["mmsi"],
            row["route"],
            DIRECTION_LABELS.get(row["direction"], row["direction"]),
            f"{row['sog']:.1f}" if row["sog"] is not None else "",
            "sent" if row["notified"] else (row["note"] or ""),
            f"{row['wind_ms']:.1f}" if row["wind_ms"] is not None else "",
            f"{row['wind_dir']:.0f}" if row["wind_dir"] is not None else "",
            num(row["gust_ms"]), num(row["water_c"]), num(row["vis_km"]), row["ice"] or "",
            num(row["ice_cm"], 0), num(row["ice_conc"], 0),
        ])
    return out.getvalue()
