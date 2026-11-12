"""The boats the route tool plans for: what each needs under the keel, how fast it goes, what
weather it takes, and for the sailing boat how fast it sails at each angle to the wind.

Every number here is a planning assumption, listed for the user in the route panel's
"Boat and planning numbers" and in the documentation. Change them here.

Sources: Buster M (2018 model) and Buster XL (current model) from buster.fi: length, beam,
weight, engine range and top speed. Draughts are not published; they are estimates with the
outboard down. The cabin boat stands for 8 m aluminium cabin boats (Ockelbo B25, Viggo C8.1
and the like). The sailing boat is a generic 35 ft cruiser with a 1.9 m fin keel; its speeds
are a typical cruising polar for that size, taken down a little for cruising trim.
"""

from __future__ import annotations

import math
from typing import Any

KN = 1852 / 3600  # m/s in a knot

# Weather limits: (okay from, bad from); visibility the other way round (km: okay below, bad below).
# shelter: how much longer open water counts than water among islands when the wind is at the
# boat's "okay" limit (0.7: 1.7 times as long), for the Automatic route. 0: the boat doesn't mind.
# fairway: how long a mile counts on a marked fairway, and on a small-craft trunk route (the
# chart's green band), against a mile of open water: small boats keep to them.
BOATS: dict[str, dict[str, Any]] = {
    "buster_m": {
        "title": "Buster M", "kind": "power", "about": "4.9 m open aluminium boat, 30-40 hp",
        "length": 4.86, "beam": 1.85, "draught": 0.5, "cruise_kn": 20, "top_kn": 26,
        "keel_margin": 0.5, "wind": (5, 8), "gust": (9, 13), "wave": (0.3, 0.6), "rain": (0.5, 2.5), "vis": (4, 1),
        "shelter": 0.7, "fairway": (0.75, 0.6),
    },
    "buster_xl": {
        "title": "Buster XL", "kind": "power", "about": "6.1 m open aluminium boat, 80-115 hp",
        "length": 6.05, "beam": 2.20, "draught": 0.6, "cruise_kn": 26, "top_kn": 36,
        "keel_margin": 0.5, "wind": (7, 11), "gust": (11, 16), "wave": (0.5, 1.0), "rain": (0.5, 3), "vis": (4, 1),
        "shelter": 0.45, "fairway": (0.78, 0.65),
    },
    "cabin8": {
        "title": "Cabin boat 8 m", "kind": "power", "about": "8 m aluminium cabin boat (Ockelbo B25, Viggo C8.1 class)",
        "length": 8.0, "beam": 2.7, "draught": 0.8, "cruise_kn": 25, "top_kn": 38,
        "keel_margin": 0.5, "wind": (9, 14), "gust": (14, 20), "wave": (0.8, 1.6), "rain": (1, 5), "vis": (3, 0.8),
        "shelter": 0.25, "fairway": (0.8, 0.7),
    },
    "sail35": {
        "title": "Sailboat 35 ft", "kind": "sail", "about": "10.7 m cruising sailboat, fin keel 1.9 m",
        "length": 10.7, "beam": 3.5, "draught": 1.9, "cruise_kn": 6, "top_kn": 8,
        "keel_margin": 0.6, "wind": (10, 15), "gust": (15, 21), "wave": (1.2, 2.2), "rain": (1, 5), "vis": (3, 0.8),
        "shelter": 0.0, "fairway": (0.85, 0.85),
    },
}
DEFAULT_BOAT = "buster_xl"
OLD_NAMES = {"small": "buster_m", "medium": "buster_xl", "large": "cabin8"}  # the boat sizes before 0.53

# --- sailing -------------------------------------------------------------------------------
# Boat speed in knots by true wind speed (knots, rows) and true wind angle (degrees off the
# bow, columns). Below the first angle the boat can't point: it tacks.
POLAR_ANGLES = (40, 52, 60, 75, 90, 110, 120, 135, 150, 180)
POLAR: dict[int, tuple[float, ...]] = {
    4: (2.2, 2.7, 2.9, 3.1, 3.2, 3.0, 2.8, 2.5, 2.1, 1.7),
    6: (3.3, 3.9, 4.2, 4.5, 4.6, 4.5, 4.3, 3.9, 3.3, 2.7),
    8: (4.3, 5.0, 5.3, 5.6, 5.8, 5.7, 5.5, 5.0, 4.4, 3.6),
    10: (5.0, 5.7, 6.0, 6.3, 6.5, 6.5, 6.3, 5.9, 5.3, 4.5),
    12: (5.5, 6.1, 6.4, 6.7, 6.9, 6.9, 6.8, 6.5, 6.0, 5.2),
    16: (5.9, 6.5, 6.8, 7.1, 7.3, 7.5, 7.5, 7.3, 6.9, 6.3),
    20: (6.0, 6.6, 6.9, 7.3, 7.6, 7.9, 8.0, 7.9, 7.5, 7.0),
    25: (5.8, 6.5, 6.9, 7.3, 7.7, 8.1, 8.3, 8.3, 8.0, 7.5),
    30: (5.4, 6.2, 6.7, 7.2, 7.6, 8.1, 8.4, 8.5, 8.3, 7.8),
}
SAIL = {
    "trim": 0.9,  # of the polar: cruising sails, a loaded boat, not racing
    "motor_kn": 5.5,  # under engine
    "motor_below_kn": 3.0,  # sailing slower than this: the engine goes on
    "motor_wind_ms": 2.0,  # under this much wind the engine is on anyway
    "tack_loss": 0.93,  # of the speed made good when tacking or gybing (time lost in the turns)
    "narrow_m": 150,  # water narrower than this to the nearest shoal: no room to tack, engine on when beating
    "reef_ms": 11.0,  # from here the boat is reefed: the polar's row is used as it is, comfort falls
    # The comfortable route counts time on these as longer than it is, so it gives way to
    # an easier point of sail or more sheltered water when that costs little.
    "comfort_beat": 1.6,  # beating
    "comfort_wind_ms": 9.0, "comfort_wind": 0.12,  # per m/s of wind over this
    "comfort_wave_m": 0.8, "comfort_wave": 0.6,  # per metre of waves over this
    "comfort_run": 1.15,  # dead downwind (rolling, gybes)
    "comfort_motor": 1.1,
}
HEADINGS = 16  # the directions the planner steers in (22.5 degrees apart)


def boat(name: str | None) -> tuple[str, dict[str, Any]]:
    name = OLD_NAMES.get(name or "", name or "")
    if name not in BOATS:
        name = DEFAULT_BOAT
    return name, BOATS[name]


def needs_depth(b: dict[str, Any]) -> float:
    """The charted depth the boat is routed over: its draught and the margin under the keel."""
    return b["draught"] + b["keel_margin"]


def _row(tws_kn: float) -> list[float]:
    keys = sorted(POLAR)
    if tws_kn <= keys[0]:
        return [v * max(0.0, tws_kn) / keys[0] for v in POLAR[keys[0]]]
    if tws_kn >= keys[-1]:
        return list(POLAR[keys[-1]])
    for a, b in zip(keys, keys[1:]):
        if a <= tws_kn <= b:
            f = (tws_kn - a) / (b - a)
            return [x + (y - x) * f for x, y in zip(POLAR[a], POLAR[b])]
    return list(POLAR[keys[-1]])


def polar_kn(tws_kn: float, twa: float) -> float:
    """Speed straight along a course `twa` degrees off the wind (0 when it can't point there)."""
    twa = abs(((twa + 180) % 360) - 180)
    if twa < POLAR_ANGLES[0]:
        return 0.0
    row = _row(tws_kn)
    for i in range(len(POLAR_ANGLES) - 1):
        a, b = POLAR_ANGLES[i], POLAR_ANGLES[i + 1]
        if a <= twa <= b:
            return (row[i] + (row[i + 1] - row[i]) * (twa - a) / (b - a)) * SAIL["trim"]
    return row[-1] * SAIL["trim"]


def best_vmg(tws_kn: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """((angle, speed made good to windward), (angle, speed made good downwind))."""
    up = max(((a, polar_kn(tws_kn, a) * math.cos(math.radians(a))) for a in range(40, 61, 2)), key=lambda t: t[1])
    down = max(((a, -polar_kn(tws_kn, a) * math.cos(math.radians(a))) for a in range(135, 181, 3)), key=lambda t: t[1])
    return up, down


def made_good(tws_ms: float, twa: float, narrow: bool = False) -> tuple[float, str]:
    """(knots made good along a course `twa` off the wind, how): "sail", "beat" (tacking
    towards it), "gybe" (gybing downwind), or "motor". Tacking between the two best close-
    hauled courses makes V cos(a) / cos(twa) good along a course inside them."""
    twa = abs(((twa + 180) % 360) - 180)
    motor = SAIL["motor_kn"]
    if tws_ms < SAIL["motor_wind_ms"]:
        return motor, "motor"
    tws = tws_ms / KN
    (up_a, up_v), (down_a, down_v) = best_vmg(tws)
    if twa < up_a:
        if narrow:
            return motor, "motor"
        v, how = up_v / max(0.2, math.cos(math.radians(twa))) * SAIL["tack_loss"], "beat"
        v = min(v, polar_kn(tws, up_a))
    else:
        v, how = polar_kn(tws, twa), "sail"
        if twa > down_a and not narrow:
            gybing = down_v / max(0.2, -math.cos(math.radians(twa))) * SAIL["tack_loss"]
            if gybing > v:
                v, how = min(gybing, polar_kn(tws, down_a)), "gybe"
    if v < SAIL["motor_below_kn"]:
        return motor, "motor"
    return v, how


def comfort_factor(how: str, twa: float, tws_ms: float, wave_m: float | None) -> float:
    """How much longer a stretch counts on the comfortable route than it takes."""
    s = SAIL
    f = 1.0
    if how == "beat":
        f *= s["comfort_beat"]
    elif how == "motor":
        f *= s["comfort_motor"]
    elif how == "gybe" or abs(((twa + 180) % 360) - 180) > 165:
        f *= s["comfort_run"]
    if tws_ms > s["comfort_wind_ms"]:
        f *= 1 + s["comfort_wind"] * (tws_ms - s["comfort_wind_ms"])
    if wave_m is not None and wave_m > s["comfort_wave_m"]:
        f *= 1 + s["comfort_wave"] * (wave_m - s["comfort_wave_m"])
    return f


def listing() -> dict[str, Any]:
    """For the panel: the boats, and every planning number in plain words."""
    return {
        "default": DEFAULT_BOAT,
        "boats": {k: {**{f: v[f] for f in ("title", "kind", "about", "length", "beam", "draught", "cruise_kn", "top_kn",
                                             "keel_margin", "wind", "gust", "wave", "rain", "vis", "shelter", "fairway")},
                      "depth": round(needs_depth(v), 1)} for k, v in BOATS.items()},
        "sail": {**SAIL, "angles": POLAR_ANGLES, "polar": {str(k): v for k, v in POLAR.items()}},
    }
