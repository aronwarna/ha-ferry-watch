"""Made-up port calls and port visits for the simulator (FW_SIMULATE=1)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .geo import distance_m
from .simulate import FLEET_SCENARIOS, KNOTS, SCENARIOS

STARTED = time.time()  # about when run_simulation starts its ships


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def _arrival(plan) -> float:
    _mmsi, path, _dock, rel, speed = plan
    length = sum(distance_m(*a, *b) for a, b in zip(path, path[1:]))
    return STARTED + rel + length / (speed * KNOTS)


def _call(n: int, mmsi: int, port: str, prev: str | None, nxt: str | None, berth: str,
          eta=None, etd=None, ata=None, atd=None) -> dict[str, Any]:
    area = {"portAreaCode": "X", "portAreaName": berth, "berthName": berth}
    for key, ts in (("eta", eta), ("etd", etd), ("ata", ata), ("atd", atd)):
        area[key] = _iso(ts) if ts else None
    return {"portCallId": 900000 + n, "portCallTimestamp": _iso(STARTED - 3600), "mmsi": mmsi,
            "portToVisit": port, "prevPort": prev, "nextPort": nxt, "portAreaDetails": [area]}


def fake_calls() -> list[dict[str, Any]]:
    s = STARTED
    plans = {p[0]: p for p in (*SCENARIOS, *FLEET_SCENARIOS)}
    dep = {m: s + p[3] for m, p in plans.items()}
    calls = [
        # Silja Serenade left Helsinki on time, Mariehamn and then Stockholm ahead.
        _call(1, 230184000, "FIHEL", "SESTO", "FIMHQ", "Olympia", eta=dep[230184000] - 5 * 3600,
              ata=dep[230184000] - 5 * 3600 + 120, etd=dep[230184000] - 60, atd=dep[230184000]),
        _call(2, 230184000, "FIMHQ", "FIHEL", "SESTO", "Mariehamn", eta=dep[230184000] + 11.25 * 3600,
              etd=dep[230184000] + 11.5 * 3600),
        # Silja Symphony coming in from Mariehamn.
        _call(3, 265004000, "FIMHQ", "SESTO", "FIHEL", "Mariehamn", ata=dep[265004000] - 8 * 3600,
              atd=dep[265004000] - 7.75 * 3600),
        _call(4, 265004000, "FIHEL", "FIMHQ", "FIMHQ", "Olympia", eta=_arrival(plans[265004000]) + 300,
              etd=_arrival(plans[265004000]) + 6.5 * 3600),
        # Viking Cinderella at Katajanokka for the day.
        _call(5, 230713000, "FIHEL", "FIMHQ", "FIMHQ", "Katajanokka", ata=s - 5 * 3600, etd=s + 2.5 * 3600),
        # Gabriella out of Katajanokka.
        _call(6, 230361000, "FIHEL", "FIMHQ", "FIMHQ", "Katajanokka", ata=dep[230361000] - 6 * 3600,
              atd=dep[230361000]),
        _call(7, 230361000, "FIMHQ", "FIHEL", "SESTO", "Mariehamn", eta=dep[230361000] + 10.75 * 3600,
              etd=dep[230361000] + 11 * 3600),
        # Tallink shuttles.
        _call(8, 276000101, "FIHEL", "EETLL", "EETLL", "Länsisatama", ata=dep[276000101] - 3600,
              atd=dep[276000101]),
        _call(9, 276000102, "FIHEL", "EETLL", "EETLL", "Länsisatama", eta=_arrival(plans[276000102]),
              etd=_arrival(plans[276000102]) + 3600),
        _call(10, 276000103, "FIHEL", "EETLL", "EETLL", "Katajanokka", eta=_arrival(plans[276000103]),
              etd=_arrival(plans[276000103]) + 2.5 * 3600),
        # A ship only in the traffic view, for the popup.
        _call(11, 538000206, "FIHEL", "DEHAM", "SESTO", "Vuosaari", eta=s + 2 * 3600, etd=s + 14 * 3600),
    ]
    return calls


async def fake_port_calls(params: dict[str, Any]) -> Any:
    calls = [c for c in fake_calls() if c["portToVisit"] == params.get("locode")]
    return {"dataUpdatedTime": _iso(time.time()), "portCalls": calls}


# Two-day rounds (UTC hours from midnight): Helsinki -> Mariehamn -> Stockholm -> Mariehamn.
SILJA_ROUND = [("Helsinki", 7.5, 14.0), ("Mariehamn", 25.25, 25.5), ("Stockholm", 31.75, 38.75),
               ("Mariehamn", 45.9, 46.15), ("Helsinki", 55.5, 62.0)]
SHUTTLE_ROUND = [("Helsinki", 0.0, 1.0), ("Tallinn", 3.0, 4.0)]


def seed_visits(db, days: int = 21) -> None:
    """Earlier port visits, so the usual crossing times and next ports have something to go on."""
    from .portcalls import VisitLog
    VisitLog(db)  # makes the table
    if db.execute("SELECT 1 FROM port_visits LIMIT 1").fetchone():
        return
    end = STARTED - 2 * 86400
    start = end - days * 86400
    day0 = start - start % 86400
    rows = []
    for k, mmsi in enumerate((230184000, 265004000, 230361000, 230713000)):
        for d in range(0, days, 2):
            base = day0 + (d + k % 2) * 86400 + (k // 2) * 1800
            for port, a, b in SILJA_ROUND[:-1]:
                if base + b * 3600 < end:
                    rows.append((mmsi, port, int(base + a * 3600), int(base + b * 3600)))
    for k, mmsi in enumerate((276000101, 276000102, 276000103)):
        t = day0 + k * 2 * 3600
        while t < end:
            for port, a, b in SHUTTLE_ROUND:
                rows.append((mmsi, port, int(t + a * 3600), int(t + b * 3600)))
            t += 6 * 3600
    rows.sort(key=lambda r: (r[0], r[2]))
    db.executemany("INSERT INTO port_visits (mmsi, port, arrive_ts, leave_ts) VALUES (?, ?, ?, ?)", rows)
    db.commit()
