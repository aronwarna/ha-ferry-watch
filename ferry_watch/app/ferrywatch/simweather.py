"""Made-up answers for the Windy-style weather layers in the simulator, so the colour layers,
isobars, waves, lightning, aurora and light pollution can be tried offline.

The weather is one invented situation that moves with time: a low crossing from the
Bothnian Sea towards Lake Ladoga with a band of cloud and rain ahead of it (snow on its
cold northern side), milder air over the sea than inland, a clearer sky with high cloud
behind it, and fog over the inland lakes at night. The sea answers follow the wind; the
atlas tiles are the real ones if FW_FAKE_LP_DIR points at them, otherwise a dark sky with
glowing towns.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import random
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .simsea import fake_forecast as _wind_forecast

T_REF = 1762300800  # 2025-11-05 00:00 UTC: the low is over the Bothnian Sea then


def _low(ts: float) -> tuple[float, float]:
    """Where the low is: drifting east-north-east about 30 km an hour, round again every 3 days."""
    h = ((ts - T_REF) / 3600) % 72
    return 61.6 + 0.012 * h, 15.5 + 0.22 * h


def weather_at(lat: float, lon: float, ts: float) -> dict[str, float]:
    clat, clon = _low(ts)
    k = math.cos(math.radians(lat))
    dx, dy = (lon - clon) * k, lat - clat
    r2 = dx * dx + dy * dy
    hour = (ts / 3600 + lon / 15) % 24  # local solar hour
    # Pressure: the low (980 hPa at its centre) on a broad 1015 hPa field, higher to the south-west.
    p = 1015 + 3 * (59.5 - lat) * 0.3 + 2 * (lon - 24) * 0.1 - 33 * math.exp(-r2 / 6)
    # Temperature: colder north and inland, a daily swing inland, the sea mild in November.
    sea = 1 if (lat < 60.3 and 21 < lon < 30) or lon < 21.5 else 0
    t = 7.5 - 1.1 * (lat - 59.5) + (0 if sea else -1.5) + (1 - sea) * 2.5 * math.sin((hour - 9) / 24 * 2 * math.pi)
    t += 3 * math.exp(-((dx - 1.5) ** 2 + (dy + 1) ** 2) / 4)  # warm sector south-east of the low
    # Cloud: a band ahead (east) of the low, high cloud further ahead, clearing behind it.
    ahead = dx * 0.8 + dy * 0.6
    band = math.exp(-((ahead - 1.5) ** 2) / 1.2)
    low = max(0, min(100, 100 * band + 20 * math.sin(lat * 3 + lon)))
    mid = max(0, min(100, 90 * math.exp(-((ahead - 2.6) ** 2) / 1.5)))
    high = max(0, min(100, 85 * math.exp(-((ahead - 4.0) ** 2) / 3) + 25 * math.exp(-((ahead + 2) ** 2) / 4)))
    cloud = 100 - (100 - low) * (100 - mid) * (100 - high) / 10000
    rain = max(0.0, 3.5 * band * math.exp(-r2 / 40) - 0.2)
    ws = 4 + 12 * math.exp(-((math.sqrt(r2) - 2.2) ** 2) / 2) + (3 if sea else 0)
    gust = ws * 1.45 + 1
    # Fog: inland lakes on clear nights.
    night = math.cos((hour - 3) / 24 * 2 * math.pi)
    fog = max(0.0, night) * (1 - cloud / 100) * (0 if sea else 1) * (0.6 + 0.4 * math.sin(lat * 7 + lon * 5))
    rh = min(100.0, 78 + 22 * fog + 10 * band)
    td = t - (100 - rh) / 5
    vis = max(150.0, 30000 * (1 - fog) ** 3 * (1 - 0.6 * band))
    # Round the low anticlockwise (and a little inwards), as a bearing the wind comes from.
    to = (math.degrees(math.atan2(-dy, dx)) + 20) % 360 if r2 > 1e-6 else 0.0
    wdir = (to + 180) % 360
    return {"Temperature": t, "Pressure": p, "WindSpeedMS": ws, "WindGust": gust, "WindDirection": wdir,
            "TotalCloudCover": cloud,
            "LowCloudCover": low, "MediumCloudCover": mid, "HighCloudCover": high, "Precipitation1h": rain,
            "Humidity": rh, "DewPoint": td, "Visibility": vis}


def fake_fmi_forecast(params: list[tuple[str, str]]) -> str:
    """An FMI multipointcoverage answer for the asked points, hours and parameters."""
    # The wind layer's answer has the right shape; its values are replaced here.
    names = dict(params)["parameters"].split(",")
    xml = _wind_forecast([(k, v if k != "parameters" else "WindSpeedMS") for k, v in params])
    start = xml.index("<gmlcov:positions>") + len("<gmlcov:positions>")
    end = xml.index("</gmlcov:positions>")
    rows = [r.split() for r in xml[start:end].split("\n") if r.strip()]
    values = []
    for lat, lon, ts in rows:
        w = weather_at(float(lat), float(lon), float(ts))
        values.append(" ".join(f"{w[n]:.1f}" if n in w else "NaN" for n in names) + " ")
    vs = xml.index("<gml:doubleOrNilReasonTupleList>") + len("<gml:doubleOrNilReasonTupleList>")
    ve = xml.index("</gml:doubleOrNilReasonTupleList>")
    fields = "".join(f'<swe:field name="{p}" xlink:href="https://opendata.fmi.fi/meta?param={p}"/>' for p in names)
    fs = xml.index("<swe:DataRecord>") + len("<swe:DataRecord>")
    fe = xml.index("</swe:DataRecord>")
    return xml[:vs] + "\n".join(values) + xml[ve:fs] + fields + xml[fe:]


async def fake_fmi_fetch(url: str, params: list[tuple[str, str]]) -> str:
    query = dict(params).get("storedquery_id", "")
    if "lightning" in query:
        return fake_lightning(params)
    return fake_fmi_forecast(params)


# --- the sea --------------------------------------------------------------------------------

def sea_at(lat: float, lon: float, ts: float) -> dict[str, float]:
    w = weather_at(lat, lon, ts)
    # The waves travel with the wind around the low.
    frm = w["WindDirection"]
    fetch = 1.0 if lon > 22 and lat < 60.5 else 0.8
    wave = max(0.1, 0.012 * w["WindSpeedMS"] ** 2 * fetch)
    hours = (ts - T_REF) / 3600
    return {"wave_height": wave, "wave_direction": frm, "wave_period": 2.5 + 1.6 * wave,
            "swell_wave_height": 0.15 + 0.1 * math.sin(lat), "swell_wave_direction": (frm + 20) % 360,
            "sea_surface_temperature": 9.5 - 0.9 * (lat - 59.5) - 0.15 * (lon - 22) + 0.2 * math.sin(hours / 24),
            "sea_level_height_msl": 0.002 * (1013 - w["Pressure"]) * 10 + 0.15 * math.sin(lon - 22 + hours / 12)
            + (0.25 if lon > 26 else 0)}


def _om(params: dict[str, str], value_fn) -> Any:
    lats = [float(v) for v in params["latitude"].split(",")]
    lons = [float(v) for v in params["longitude"].split(",")]
    names = params["hourly"].split(",")
    now = int(datetime.now(timezone.utc).timestamp() // 3600 * 3600)
    times = [now + i * 3600 for i in range(int(params.get("forecast_hours", 49)))]
    out = []
    for la, lo in zip(lats, lons):
        hourly: dict[str, list] = {"time": times}
        cols = [value_fn(la, lo, ts) for ts in times]
        for n in names:
            hourly[n] = [round(c[n], 2) if n in c else None for c in cols]
        out.append({"latitude": la, "longitude": lo, "hourly": hourly})
    return out if len(out) > 1 else out[0]


async def fake_marine_json(url: str, params: dict[str, str]) -> Any:
    return _om(params, sea_at)


# --- lightning ----------------------------------------------------------------------------

def fake_lightning(params: list[tuple[str, str]]) -> str:
    """Strikes in the rain band ahead of the low over the last hour."""
    q = dict(params)
    end = int(datetime.fromisoformat(q["endtime"].replace("Z", "+00:00")).timestamp())
    rnd = random.Random(end // 300)
    rows, values = [], []
    for _ in range(140):
        ago = rnd.random() ** 1.5 * 3600
        ts = end - ago
        clat, clon = _low(ts)
        a = rnd.gauss(0, 0.4)
        lat = clat - 1.2 + rnd.gauss(0, 0.35) + a * 0.6
        lon = clon + 2.6 + rnd.gauss(0, 0.6) - a
        rows.append(f"{lat:.4f} {lon:.4f}  {int(ts)} ")
        values.append(f"{rnd.choice((-1, -1, -1, 1)) * rnd.uniform(5, 60):.1f} {1 if rnd.random() < 0.3 else 0} ")
    xml = _wind_forecast([("parameters", "WindSpeedMS"), ("starttime", q["starttime"]), ("endtime", q["starttime"]),
                          ("timestep", "60"), ("latlon", "60,24")])
    s, e = xml.index("<gmlcov:positions>") + 18, xml.index("</gmlcov:positions>")
    xml = xml[:s] + "\n".join(rows) + xml[e:]
    s, e = xml.index("<gml:doubleOrNilReasonTupleList>") + 32, xml.index("</gml:doubleOrNilReasonTupleList>")
    xml = xml[:s] + "\n".join(values) + xml[e:]
    s, e = xml.index("<swe:DataRecord>") + 16, xml.index("</swe:DataRecord>")
    return xml[:s] + '<swe:field name="peak_current"/><swe:field name="cloud_indicator"/>' + xml[e:]


# --- aurora -------------------------------------------------------------------------------

def fake_ovation(now: float) -> str:
    """An aurora oval over Lapland, wider at night."""
    t = datetime.fromtimestamp(now, timezone.utc)
    rows = []
    for lon in range(0, 360):
        for lat in range(50, 76):
            lo = lon if lon <= 180 else lon - 360
            midnight = math.cos(math.radians(lo + t.hour * 15 + t.minute / 4 - 180))  # 1 at local midnight
            centre = 66.5 - 1.5 * midnight
            v = max(0, 60 * math.exp(-((lat - centre) ** 2) / (6 + 3 * midnight)) * (0.6 + 0.4 * midnight))
            rows.append(f"[{lon}, {lat}, {round(v)}]")
    return ('{"Observation Time": "%s", "Forecast Time": "%s", "Data Format": "[Longitude, Latitude, Aurora]", '
            '"coordinates": [%s], "type": "MultiPoint"}' % (t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                            t.strftime("%Y-%m-%dT%H:%M:%SZ"), ", ".join(rows)))


def fake_kp(now: float) -> str:
    start = int(now // 10800 * 10800) - 2 * 86400
    rows = [["time_tag", "kp", "observed", "noaa_scale"]]
    for i in range(5 * 8):
        ts = start + i * 10800
        kp = 2.3 + 2.2 * math.exp(-(((ts - now) / 3600 - 10) ** 2) / 40)
        tag = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rows.append([tag, f"{kp:.2f}", "observed" if ts < now else "predicted", None])
    return json.dumps(rows)


async def fake_noaa_get(url: str, params: Any = None) -> str:
    now = datetime.now(timezone.utc).timestamp()
    return fake_kp(now) if "k-index" in url else fake_ovation(now)


# --- light pollution ----------------------------------------------------------------------

TOWNS = [(60.17, 24.94, 45), (60.45, 22.27, 25), (61.50, 23.76, 30), (59.44, 24.75, 40), (59.33, 18.07, 55),
         (60.98, 25.66, 15), (62.24, 25.75, 18), (60.47, 26.95, 8), (60.39, 25.66, 6), (59.98, 24.42, 1.2),
         (61.06, 28.19, 10), (60.71, 28.75, 12), (60.0, 30.3, 70), (58.38, 26.72, 15)]


def _fake_lp_tile(tx: int, ty: int) -> bytes:
    """An atlas tile in its own format: a dark sky with towns glowing."""
    from .lightpollution import N, RES
    west, south = 5 * (tx - 1) - 180, 5 * (ty - 1) - 65
    vals = []
    for r in range(N):
        lat = south + (r + 0.5) / RES
        k = math.cos(math.radians(lat))
        row = []
        for c in range(0, N, 1):
            lon = west + (c + 0.5) / RES
            ratio = 0.02
            for tlat, tlon, strength in TOWNS:
                d2 = ((lat - tlat) ** 2 + ((lon - tlon) * k) ** 2) * 111 ** 2
                ratio += strength / (1 + d2 / 25)
            row.append(max(0, min(16000, round(math.log(ratio * 195 / 5 + 1) / 0.0195))))
        vals.append(row)
    data = array("b", [0]) * (N * N + 1)
    first = vals[0][0]
    data[0], data[1] = first // 128, first % 128
    for r in range(1, N):
        data[N * r + 1] = max(-128, min(127, vals[r][0] - vals[r - 1][0]))
    for r in range(N):
        for c in range(1, N):
            data[N * r + 1 + c] = max(-128, min(127, vals[r][c] - vals[r][c - 1]))
    return gzip.compress(data.tobytes())


async def fake_lp_get(url: str) -> bytes:
    name = url.rsplit("/", 1)[1]
    folder = os.environ.get("FW_FAKE_LP_DIR")
    if folder and (Path(folder) / name).exists():
        return (Path(folder) / name).read_bytes()
    tx, ty = (int(v) for v in name.split(".")[0].split("_")[2:4])
    return _fake_lp_tile(tx, ty)


# --- terrain for the sunset spots ----------------------------------------------------------

def height_at(lat: float, lon: float) -> float:
    """Made-up heights: the Gulf's north shore wiggling along 60.05 N, rocky hills inland,
    islands off it, the sea floor going down southwards."""
    coast = 60.05 + 0.03 * math.sin(lon * 9) + 0.012 * math.sin(lon * 41 + 1)
    if lat < coast:
        islands = 14 * math.sin(lat * 260) * math.sin(lon * 140) - 4 - (coast - lat) * 120
        return islands if islands > 0 else min(-1.0, -2 - (coast - lat) * 400)
    up = (lat - coast) * 900  # rising inland, 0 at the shore
    hills = 18 * (0.5 + 0.5 * math.sin(lat * 170) * math.sin(lon * 95)) + 9 * math.sin(lat * 520 + lon * 300)
    return max(0.5, min(25.0, up) * 0.6 + hills * min(1.0, up / 6))


def terrain_tile(z: int, x: int, y: int) -> bytes:
    from .simoverlays import png
    size, n = 256, 2 ** z
    lons = [(x + (c + 0.5) / size) / n * 360 - 180 for c in range(size)]
    px = bytearray()
    for r in range(size):
        merc = math.pi * (1 - 2 * (y + (r + 0.5) / size) / n)
        lat = math.degrees(math.atan(math.sinh(merc)))
        for lon in lons:
            v = height_at(lat, lon) + 32768
            red = int(v // 256)
            green = int(v % 256)
            blue = int((v - math.floor(v)) * 256)
            px.extend((red, green, blue, 255))
    return png(size, size, bytes(px))
