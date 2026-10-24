"""Simulator stand-ins (FW_SIMULATE=1) for the weather stations and harbours: FMI
multipointcoverage answers and an Overpass answer in their documented formats, with
made-up values. Kept apart from simulate.py, whose chart part is edited elsewhere.
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any

# (fmisid, name, lat, lon, typical wind m/s); one inland station to show it's left out.
SIM_STATIONS = [
    (100997, "Kirkkonummi Mäkiluoto", 59.9198, 24.3502, 8.0), (100996, "Helsinki Harmaja", 60.1051, 24.9754, 7.0),
    (100971, "Helsinki Kaivopuisto", 60.1533, 24.9570, 5.0), (101022, "Porvoo Kalbådagrund", 59.9861, 25.5989, 9.5),
    (101039, "Loviisa Orrengrund", 60.2744, 26.4448, 7.5), (101042, "Kotka Haapasaari", 60.2878, 27.1849, 8.5),
    (100932, "Hanko Russarö", 59.7737, 22.9480, 9.0), (100965, "Raasepori Jussarö", 59.8209, 23.5718, 8.0),
    (100969, "Inkoo Bågaskär", 59.9311, 24.0140, 8.5), (100908, "Parainen Utö", 59.7791, 21.3745, 11.0),
    (100921, "Kökar Bogskär", 59.5032, 20.3500, 12.0), (100928, "Kumlinge kirkonkylä", 60.2614, 20.7506, 6.0),
    (100909, "Lemland Nyhamn", 59.9589, 19.9536, 10.0), (101059, "Kustavi Isokari", 60.7195, 21.0262, 9.0),
    (101061, "Rauma Kylmäpihlaja", 61.1450, 21.3036, 9.5), (101267, "Pori Tahkoluoto", 61.6276, 21.3833, 8.5),
    (101268, "Kristiinankaupunki Majakka", 62.2027, 21.1730, 8.0), (101481, "Mustasaari Valassaaret", 63.4342, 21.0681, 10.0),
    (101661, "Kokkola Tankar", 63.9519, 22.8492, 9.0), (101673, "Kalajoki Ulkokalla", 64.3290, 23.4483, 10.5),
    (101784, "Hailuoto Marjaniemi", 65.0401, 24.5590, 9.0), (101846, "Kemi Ajos", 65.6720, 24.5160, 7.0),
    (101339, "Jyväskylä lentoasema", 62.3997, 25.6783, 3.0),
]
SIM_WAVE = [(134221, "Suomenlahti aaltopoiju", 59.9650, 25.2350)]
SIM_SEA = [(132310, "Helsinki Kaivopuisto", 60.1533, 24.9564), (134253, "Hanko Pikku Kolalahti", 59.8225, 22.9765)]


def _params(params: dict[str, Any]) -> tuple[list[str], float, int]:
    names = [p for p in str(params.get("parameters", "")).split(",") if p]
    start = datetime.fromisoformat(str(params.get("starttime")).replace("Z", "+00:00")).timestamp()
    return names, start, int(params.get("timestep", 10)) * 60


def _value(name: str, fmisid: int, base: float, lat: float, ts: float) -> float:
    phase = fmisid % 17
    drift = math.sin(ts / 9000 + phase)
    gusty = 1.0 + 0.15 * math.sin(ts / 1300 + phase)
    if name == "windspeedms":
        return max(0.0, base * gusty + 2.5 * drift)
    if name == "windgust":
        return max(0.0, base * gusty + 2.5 * drift) * 1.4 + 1
    if name == "winddirection":
        return (210 + 40 * drift + phase * 3) % 360
    if name == "temperature":
        return 9.5 - (lat - 60) * 1.5 + 1.2 * math.sin(ts / 14000)
    if name == "WaveHs":
        return 0.6 + 0.4 * (drift + 1)
    if name in ("TWATER", "TW_PT1H_AVG"):
        return 11.8 - (lat - 60) * 1.2
    return math.nan


def fake_multipoint(params: dict[str, Any], now: float | None = None) -> str:
    """An FMI multipointcoverage answer for the asked stored query, parameters and times."""
    query = str(params.get("storedquery_id", ""))
    if "wave" in query:
        sites = [(f, n, la, lo, 0.0) for f, n, la, lo in SIM_WAVE]
    elif "mareograph" in query:
        sites = [(f, n, la, lo, 0.0) for f, n, la, lo in SIM_SEA]
    else:
        sites = SIM_STATIONS
    names, start, step = _params(params)
    now = now or time.time()
    first = start - start % step + step
    times = [t for t in range(int(first), int(now), step)]
    locations, points, rows, values = [], [], [], []
    for fmisid, name, lat, lon, base in sites:
        locations.append(
            f'<target:member><target:Location gml:id="obsloc-fmisid-{fmisid}-pos">'
            f'<gml:identifier codeSpace="http://xml.fmi.fi/namespace/stationcode/fmisid">{fmisid}</gml:identifier>'
            f'<gml:name codeSpace="http://xml.fmi.fi/namespace/locationcode/name">{name}</gml:name>'
            f'<gml:name codeSpace="http://xml.fmi.fi/namespace/locationcode/geoid">-{fmisid}</gml:name>'
            f'<target:representativePoint xlink:href="#point-{fmisid}"/>'
            f'<target:region codeSpace="http://xml.fmi.fi/namespace/location/region">Sim</target:region>'
            "</target:Location></target:member>")
        points.append(f'<gml:pointMember><gml:Point gml:id="point-{fmisid}" srsDimension="2" '
                      'srsName="http://www.opengis.net/def/crs/EPSG/0/4258">'
                      f"<gml:name>{name}</gml:name><gml:pos>{lat:.5f} {lon:.5f} </gml:pos></gml:Point></gml:pointMember>")
        for ts in times:
            rows.append(f"{lat:.5f} {lon:.5f}  {ts} ")
            vals = []
            for p in names:
                v = _value(p, fmisid, base, lat, ts)
                # Some stations have no gust sensor, and the odd value goes missing.
                if (p == "windgust" and fmisid == 100928) or (ts // step + fmisid) % 23 == 0:
                    v = math.nan
                vals.append("NaN" if math.isnan(v) else f"{v:.1f}")
            values.append(" ".join(vals) + " ")
    fields = "".join(f'<swe:field name="{p}" xlink:href="https://opendata.fmi.fi/meta?param={p}"/>' for p in names)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<wfs:FeatureCollection timeStamp="2026-10-19T08:41:12Z" numberMatched="1" numberReturned="1" '
        'xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:om="http://www.opengis.net/om/2.0" '
        'xmlns:omso="http://inspire.ec.europa.eu/schemas/omso/3.0" xmlns:sam="http://www.opengis.net/sampling/2.0" '
        'xmlns:sams="http://www.opengis.net/samplingSpatial/2.0" xmlns:gmlcov="http://www.opengis.net/gmlcov/1.0" '
        'xmlns:swe="http://www.opengis.net/swe/2.0" xmlns:target="http://xml.fmi.fi/namespace/om/atmosphericfeatures/1.1">'
        '<wfs:member><omso:GridSeriesObservation gml:id="obs-obs-1-1">'
        '<om:featureOfInterest><sams:SF_SpatialSamplingFeature gml:id="sampling-feature-1-1-fmisid">'
        '<sam:sampledFeature><target:LocationCollection gml:id="sampled-target-1-1">'
        + "".join(locations) +
        '</target:LocationCollection></sam:sampledFeature>'
        '<sams:shape><gml:MultiPoint gml:id="mp-1-1-fmisid">' + "".join(points) + '</gml:MultiPoint></sams:shape>'
        '</sams:SF_SpatialSamplingFeature></om:featureOfInterest>'
        '<om:result><gmlcov:MultiPointCoverage gml:id="mpcv-1-1-fmisid"><gml:domainSet>'
        '<gmlcov:SimpleMultiPoint gml:id="mp-1-1-fmisid" srsDimension="3" '
        'srsName="http://xml.fmi.fi/gml/crs/compoundCRS.php?crs=4258&amp;time=unixtime">'
        '<gmlcov:positions>' + "\n".join(rows) + '</gmlcov:positions></gmlcov:SimpleMultiPoint></gml:domainSet>'
        '<gml:rangeSet><gml:DataBlock><gml:rangeParameters/><gml:doubleOrNilReasonTupleList>'
        + "\n".join(values) +
        '</gml:doubleOrNilReasonTupleList></gml:DataBlock></gml:rangeSet>'
        '<gml:coverageFunction><gml:CoverageMappingRule><gml:ruleDefinition>Linear</gml:ruleDefinition>'
        '</gml:CoverageMappingRule></gml:coverageFunction>'
        '<gmlcov:rangeType><swe:DataRecord>' + fields + '</swe:DataRecord></gmlcov:rangeType>'
        '</gmlcov:MultiPointCoverage></om:result></omso:GridSeriesObservation></wfs:member></wfs:FeatureCollection>')


async def fake_stations_fetch(params: dict[str, Any]) -> str:
    return fake_multipoint(params)


def fake_overpass() -> dict[str, Any]:
    """An Overpass `out center tags` answer: harbours around Porkkala, the same marina mapped
    twice, and a lake marina inland (left out)."""
    return {"version": 0.6, "generator": "Overpass API (simulated)", "elements": [
        {"type": "way", "id": 1001, "center": {"lat": 59.9833, "lon": 24.425},
         "tags": {"leisure": "marina", "name": "Porkkalan Marina Dragesviken", "seamark:type": "harbour",
                  "seamark:harbour:category": "marina", "website": "https://www.example.fi/dragesviken",
                  "phone": "+358 9 123 4567", "fuel": "yes", "toilets": "yes", "shower": "yes", "sauna": "yes",
                  "electricity": "yes", "drinking_water": "yes", "waste_disposal": "yes", "restaurant": "yes",
                  "depth": "2.5"}},
        {"type": "node", "id": 2001, "lat": 59.9836, "lon": 24.4262,
         "tags": {"seamark:type": "harbour", "seamark:name": "Porkkalan Marina Dragesviken",
                  "seamark:small_craft_facility:category": "slipway;visitor_berth"}},
        {"type": "node", "id": 2002, "lat": 59.9330, "lon": 24.3800,
         "tags": {"seamark:type": "harbour", "name": "Rönnskär", "seamark:harbour:category": "marina_no_facilities",
                  "toilets": "yes", "sauna": "yes", "guest_berths": "6"}},
        {"type": "way", "id": 1002, "center": {"lat": 60.0480, "lon": 24.3810},
         "tags": {"leisure": "marina", "name": "Kantvikin venesatama", "fuel": "yes", "toilets": "yes",
                  "electricity": "yes", "slipway": "yes", "maxdraught": "1.8", "contact:website": "kantvik.example.fi"}},
        {"type": "way", "id": 1003, "center": {"lat": 60.0390, "lon": 24.5290},
         "tags": {"harbour": "yes", "name": "Upinniemen satama", "seamark:harbour:category": "naval"}},
        {"type": "way", "id": 1004, "center": {"lat": 60.2500, "lon": 24.0700},
         "tags": {"leisure": "marina", "name": "Lohjan satama"}},
    ]}


async def fake_overpass_fetch(query: str) -> dict[str, Any]:
    return fake_overpass()
